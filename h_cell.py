# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Module implementing RNN Cells.

This module provides a number of basic commonly used RNN cells, such as LSTM
(Long Short Term Memory) or GRU (Gated Recurrent Unit), and a number of
operators that allow adding dropouts, projections, or embeddings for inputs.
Constructing multi-layer cells is supported by the class `MultiRNNCell`, or by
calling the `rnn` ops several times.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import hashlib
import numbers

from tensorflow.python.eager import context
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util
from tensorflow.python.layers import base as base_layer
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import partitioned_variables
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import variables as tf_variables
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.util import nest
import tensorflow as tf

_BIAS_VARIABLE_NAME = "bias"
_WEIGHTS_VARIABLE_NAME = "kernel"


def _like_rnncell(cell):
  """Checks that a given object is an RNNCell by using duck typing."""
  conditions = [hasattr(cell, "output_size"), hasattr(cell, "state_size"),
                hasattr(cell, "zero_state"), callable(cell)]
  return all(conditions)


def _concat(prefix, suffix, static=False):
  """Concat that enables int, Tensor, or TensorShape values.

  This function takes a size specification, which can be an integer, a
  TensorShape, or a Tensor, and converts it into a concatenated Tensor
  (if static = False) or a list of integers (if static = True).

  Args:
    prefix: The prefix; usually the batch size (and/or time step size).
      (TensorShape, int, or Tensor.)
    suffix: TensorShape, int, or Tensor.
    static: If `True`, return a python list with possibly unknown dimensions.
      Otherwise return a `Tensor`.

  Returns:
    shape: the concatenation of prefix and suffix.

  Raises:
    ValueError: if `suffix` is not a scalar or vector (or TensorShape).
    ValueError: if prefix or suffix was `None` and asked for dynamic
      Tensors out.
  """
  if isinstance(prefix, ops.Tensor):
    p = prefix
    p_static = tensor_util.constant_value(prefix)
    if p.shape.ndims == 0:
      p = array_ops.expand_dims(p, 0)
    elif p.shape.ndims != 1:
      raise ValueError("prefix tensor must be either a scalar or vector, "
                       "but saw tensor: %s" % p)
  else:
    p = tensor_shape.as_shape(prefix)
    p_static = p.as_list() if p.ndims is not None else None
    p = (constant_op.constant(p.as_list(), dtype=dtypes.int32)
         if p.is_fully_defined() else None)
  if isinstance(suffix, ops.Tensor):
    s = suffix
    s_static = tensor_util.constant_value(suffix)
    if s.shape.ndims == 0:
      s = array_ops.expand_dims(s, 0)
    elif s.shape.ndims != 1:
      raise ValueError("suffix tensor must be either a scalar or vector, "
                       "but saw tensor: %s" % s)
  else:
    s = tensor_shape.as_shape(suffix)
    s_static = s.as_list() if s.ndims is not None else None
    s = (constant_op.constant(s.as_list(), dtype=dtypes.int32)
         if s.is_fully_defined() else None)

  if static:
    shape = tensor_shape.as_shape(p_static).concatenate(s_static)
    shape = shape.as_list() if shape.ndims is not None else None
  else:
    if p is None or s is None:
      raise ValueError("Provided a prefix or suffix of None: %s and %s"
                       % (prefix, suffix))
    shape = array_ops.concat((p, s), 0)
  return shape


def _zero_state_tensors(state_size, batch_size, dtype):
  """Create tensors of zeros based on state_size, batch_size, and dtype."""
  def get_state_shape(s):
    """Combine s with batch_size to get a proper tensor shape."""
    c = _concat(batch_size, s)
    size = array_ops.zeros(c, dtype=dtype)
    if context.in_graph_mode():
      c_static = _concat(batch_size, s, static=True)
      size.set_shape(c_static)
    return size
  return nest.map_structure(get_state_shape, state_size)


class RNNCell(base_layer.Layer):
  """Abstract object representing an RNN cell.

  Every `RNNCell` must have the properties below and implement `call` with
  the signature `(output, next_state) = call(input, state)`.  The optional
  third input argument, `scope`, is allowed for backwards compatibility
  purposes; but should be left off for new subclasses.

  This definition of cell differs from the definition used in the literature.
  In the literature, 'cell' refers to an object with a single scalar output.
  This definition refers to a horizontal array of such units.

  An RNN cell, in the most abstract setting, is anything that has
  a state and performs some operation that takes a matrix of inputs.
  This operation results in an output matrix with `self.output_size` columns.
  If `self.state_size` is an integer, this operation also results in a new
  state matrix with `self.state_size` columns.  If `self.state_size` is a
  (possibly nested tuple of) TensorShape object(s), then it should return a
  matching structure of Tensors having shape `[batch_size].concatenate(s)`
  for each `s` in `self.batch_size`.
  """

  def __call__(self, inputs, state, scope=None):
    """Run this RNN cell on inputs, starting from the given state.

    Args:
      inputs: `2-D` tensor with shape `[batch_size x input_size]`.
      state: if `self.state_size` is an integer, this should be a `2-D Tensor`
        with shape `[batch_size x self.state_size]`.  Otherwise, if
        `self.state_size` is a tuple of integers, this should be a tuple
        with shapes `[batch_size x s] for s in self.state_size`.
      scope: VariableScope for the created subgraph; defaults to class name.

    Returns:
      A pair containing:

      - Output: A `2-D` tensor with shape `[batch_size x self.output_size]`.
      - New state: Either a single `2-D` tensor, or a tuple of tensors matching
        the arity and shapes of `state`.
    """
    if scope is not None:
      with vs.variable_scope(scope,
                             custom_getter=self._rnn_get_variable) as scope:
        return super(RNNCell, self).__call__(inputs, state, scope=scope)
    else:
      with vs.variable_scope(vs.get_variable_scope(),
                             custom_getter=self._rnn_get_variable):
        return super(RNNCell, self).__call__(inputs, state)

  def _rnn_get_variable(self, getter, *args, **kwargs):
    variable = getter(*args, **kwargs)
    if context.in_graph_mode():
      trainable = (variable in tf_variables.trainable_variables() or
                   (isinstance(variable, tf_variables.PartitionedVariable) and
                    list(variable)[0] in tf_variables.trainable_variables()))
    else:
      trainable = variable._trainable  # pylint: disable=protected-access
    if trainable and variable not in self._trainable_weights:
      self._trainable_weights.append(variable)
    elif not trainable and variable not in self._non_trainable_weights:
      self._non_trainable_weights.append(variable)
    return variable

  @property
  def state_size(self):
    """size(s) of state(s) used by this cell.

    It can be represented by an Integer, a TensorShape or a tuple of Integers
    or TensorShapes.
    """
    raise NotImplementedError("Abstract method")

  @property
  def output_size(self):
    """Integer or TensorShape: size of outputs produced by this cell."""
    raise NotImplementedError("Abstract method")

  def build(self, _):
    # This tells the parent Layer object that it's OK to call
    # self.add_variable() inside the call() method.
    pass

  def zero_state(self, batch_size, dtype):
    """Return zero-filled state tensor(s).

    Args:
      batch_size: int, float, or unit Tensor representing the batch size.
      dtype: the data type to use for the state.

    Returns:
      If `state_size` is an int or TensorShape, then the return value is a
      `N-D` tensor of shape `[batch_size x state_size]` filled with zeros.

      If `state_size` is a nested list or tuple, then the return value is
      a nested list or tuple (of the same structure) of `2-D` tensors with
      the shapes `[batch_size x s]` for each s in `state_size`.
    """
    with ops.name_scope(type(self).__name__ + "ZeroState", values=[batch_size]):
      state_size = self.state_size
      return _zero_state_tensors(state_size, batch_size, dtype)


_LSTMStateTuple = collections.namedtuple("LSTMStateTuple", ("c", "h"))


class LSTMStateTuple(_LSTMStateTuple):
  """Tuple used by LSTM Cells for `state_size`, `zero_state`, and output state.

  Stores two elements: `(c, h)`, in that order. Where `c` is the hidden state
  and `h` is the output.

  Only used when `state_is_tuple=True`.
  """
  __slots__ = ()

  @property
  def dtype(self):
    (c, h) = self
    if c.dtype != h.dtype:
      raise TypeError("Inconsistent internal state: %s vs %s" %
                      (str(c.dtype), str(h.dtype)))
    return c.dtype




class LSTMCell(RNNCell):
  """Long short-term memory unit (LSTM) recurrent network cell.

  The default non-peephole implementation is based on:

    http://www.bioinf.jku.at/publications/older/2604.pdf

  S. Hochreiter and J. Schmidhuber.
  "Long Short-Term Memory". Neural Computation, 9(8):1735-1780, 1997.

  The peephole implementation is based on:
  https://research.google.com/pubs/archive/43905.pdf

  Hasim Sak, Andrew Senior, and Francoise Beaufays.
  "Long short-term memory recurrent neural network architectures for
   large scale acoustic modeling." INTERSPEECH, 2014.

  The class uses optional peep-hole connections, optional cell clipping, and
  an optional projection layer.
  """

  def __init__(self, num_units,
               use_peepholes=False, cell_clip=None,
               initializer=None, num_proj=None, proj_clip=None,
               num_unit_shards=None, num_proj_shards=None,
               forget_bias=1.0, state_is_tuple=True,
               activation=None, reuse=None):
    """Initialize the parameters for an LSTM cell.

    Args:
      num_units: int, The number of units in the LSTM cell.
      use_peepholes: bool, set True to enable diagonal/peephole connections.
      cell_clip: (optional) A float value, if provided the cell state is clipped
        by this value prior to the cell output activation.
      initializer: (optional) The initializer to use for the weight and
        projection matrices.
      num_proj: (optional) int, The output dimensionality for the projection
        matrices.  If None, no projection is performed.
      proj_clip: (optional) A float value.  If `num_proj > 0` and `proj_clip` is
        provided, then the projected values are clipped elementwise to within
        `[-proj_clip, proj_clip]`.
      num_unit_shards: Deprecated, will be removed by Jan. 2017.
        Use a variable_scope partitioner instead.
      num_proj_shards: Deprecated, will be removed by Jan. 2017.
        Use a variable_scope partitioner instead.
      forget_bias: Biases of the forget gate are initialized by default to 1
        in order to reduce the scale of forgetting at the beginning of
        the training. Must set it manually to `0.0` when restoring from
        CudnnLSTM trained checkpoints.
      state_is_tuple: If True, accepted and returned states are 2-tuples of
        the `c_state` and `m_state`.  If False, they are concatenated
        along the column axis.  This latter behavior will soon be deprecated.
      activation: Activation function of the inner states.  Default: `tanh`.
      reuse: (optional) Python boolean describing whether to reuse variables
        in an existing scope.  If not `True`, and the existing scope already has
        the given variables, an error is raised.

      When restoring from CudnnLSTM-trained checkpoints, must use
      CudnnCompatibleLSTMCell instead.
    """
    super(LSTMCell, self).__init__(_reuse=reuse)
    if not state_is_tuple:
      logging.warn("%s: Using a concatenated state is slower and will soon be "
                   "deprecated.  Use state_is_tuple=True.", self)
    if num_unit_shards is not None or num_proj_shards is not None:
      logging.warn(
          "%s: The num_unit_shards and proj_unit_shards parameters are "
          "deprecated and will be removed in Jan 2017.  "
          "Use a variable scope with a partitioner instead.", self)

    self._num_units = num_units
    self._use_peepholes = use_peepholes
    self._cell_clip = cell_clip
    self._initializer = initializer
    self._num_proj = num_proj
    self._proj_clip = proj_clip
    self._num_unit_shards = num_unit_shards
    self._num_proj_shards = num_proj_shards
    self._forget_bias = forget_bias
    self._state_is_tuple = state_is_tuple
    self._activation = activation or math_ops.tanh

    if num_proj:
      self._state_size = (
          LSTMStateTuple(num_units, num_proj)
          if state_is_tuple else num_units + num_proj)
      self._output_size = num_proj
    else:
      self._state_size = (
          LSTMStateTuple(num_units, num_units)
          if state_is_tuple else 2 * num_units)
      self._output_size = num_units
    self._linear1 = None
    self._linear2 = None
    if self._use_peepholes:
      self._w_f_diag = None
      self._w_i_diag = None
      self._w_o_diag = None

  @property
  def state_size(self):
    return self._state_size

  @property
  def output_size(self):
    return self._output_size

  def call(self, inputs, state):
    """Run one step of LSTM.

    Args:
      inputs: input Tensor, 2D, batch x num_units.
      state: if `state_is_tuple` is False, this must be a state Tensor,
        `2-D, batch x state_size`.  If `state_is_tuple` is True, this must be a
        tuple of state Tensors, both `2-D`, with column sizes `c_state` and
        `m_state`.

    Returns:
      A tuple containing:

      - A `2-D, [batch x output_dim]`, Tensor representing the output of the
        LSTM after reading `inputs` when previous state was `state`.
        Here output_dim is:
           num_proj if num_proj was set,
           num_units otherwise.
      - Tensor(s) representing the new state of LSTM after reading `inputs` when
        the previous state was `state`.  Same type and shape(s) as `state`.

    Raises:
      ValueError: If input size cannot be inferred from inputs via
        static shape inference.
    """
    num_proj = self._num_units if self._num_proj is None else self._num_proj
    sigmoid = math_ops.sigmoid

    if self._state_is_tuple:
      (c_prev, m_prev) = state
    else:
      c_prev = array_ops.slice(state, [0, 0], [-1, self._num_units])
      m_prev = array_ops.slice(state, [0, self._num_units], [-1, num_proj])

    dtype = inputs.dtype
    input_size = inputs.get_shape().with_rank(2)[1]
    if input_size.value is None:
      raise ValueError("Could not infer input size from inputs.get_shape()[-1]")
    if self._linear1 is None:
      scope = vs.get_variable_scope()
      with vs.variable_scope(
          scope, initializer=self._initializer) as unit_scope:
        if self._num_unit_shards is not None:
          unit_scope.set_partitioner(
              partitioned_variables.fixed_size_partitioner(
                  self._num_unit_shards))
        self._linear1 = _Linear([inputs, m_prev], 4 * self._num_units, True)

    # i = input_gate, j = new_input, f = forget_gate, o = output_gate
    lstm_matrix = self._linear1([inputs, m_prev])
    i, j, f, o = array_ops.split(
        value=lstm_matrix, num_or_size_splits=4, axis=1)
    # Diagonal connections
    if self._use_peepholes and not self._w_f_diag:
      scope = vs.get_variable_scope()
      with vs.variable_scope(
          scope, initializer=self._initializer) as unit_scope:
        with vs.variable_scope(unit_scope):
          self._w_f_diag = vs.get_variable(
              "w_f_diag", shape=[self._num_units], dtype=dtype)
          self._w_i_diag = vs.get_variable(
              "w_i_diag", shape=[self._num_units], dtype=dtype)
          self._w_o_diag = vs.get_variable(
              "w_o_diag", shape=[self._num_units], dtype=dtype)

    if self._use_peepholes:
      c = (sigmoid(f + self._forget_bias + self._w_f_diag * c_prev) * c_prev +
           sigmoid(i + self._w_i_diag * c_prev) * self._activation(j))
    else:
      c = (sigmoid(f + self._forget_bias) * c_prev + sigmoid(i) *
           self._activation(j))

    if self._cell_clip is not None:
      # pylint: disable=invalid-unary-operand-type
      c = clip_ops.clip_by_value(c, -self._cell_clip, self._cell_clip)
      # pylint: enable=invalid-unary-operand-type
    
    if self._use_peepholes:
      m = sigmoid(o + self._w_o_diag * c) * self._activation(c)
    else:
      m = sigmoid(o) * self._activation(c)

    if self._num_proj is not None:
      if self._linear2 is None:
        scope = vs.get_variable_scope()
        with vs.variable_scope(scope, initializer=self._initializer):
          with vs.variable_scope("projection") as proj_scope:
            if self._num_proj_shards is not None:
              proj_scope.set_partitioner(
                  partitioned_variables.fixed_size_partitioner(
                      self._num_proj_shards))
            self._linear2 = _Linear(m, self._num_proj, False)
      m = self._linear2(m)

      if self._proj_clip is not None:
        # pylint: disable=invalid-unary-operand-type
        m = clip_ops.clip_by_value(m, -self._proj_clip, self._proj_clip)
        # pylint: enable=invalid-unary-operand-type

    new_state = (LSTMStateTuple(c, m) if self._state_is_tuple else
                 array_ops.concat([c, m], 1))
    return m, new_state




class LN_LSTMCell(RNNCell):
  """
    Extension of LSTM recurrent network cell to
    Layer normalization cell with peepholes, peephole also normalized
    https://arxiv.org/abs/1607.06450
  """
  def __init__(self, num_units, 
               use_peepholes=False,
               cell_clip=None,
               initializer=None,
               num_proj=None,
               proj_clip=None,
               forget_bias=1.0,
               state_is_tuple=True,
               activation=None,
               reuse=None):
    """
      Initialize params

      Args:
        num_units: int, Number of units in the LSTM cell.
        use_peepholes: bool, True to enable diagonal/peephole connections.
        cell_clip: (optional) float, if provided the cell state is clipped by this value prior to the cell output activation.
        initializer: (optional) , Initializer to use for the weight and projection matrics.
        num_proj: (optional) int, Output dimensionality for the projection matrics, If none, no projection is performed.
        proj_clip: (optional) float, If 'num_proj' > 0 and 'proj_clip' is provided, then the projected values are clipped elementwise to within '[-proj_clip, proj_clip]'
        forget_bias: float, Bias of the forget gate are initialized by default to 1.0 in order to reduce the scale of forgetting at the beginning of the training. Must set it manually to '0.0' when restoring CudnnLSTM trained checkpoint. 
        state_is_tuple: bool, if True, accepted and returned states are 2-tuples of the 'c_state' and 'm_state'. If False, the are concatenated along the comumn axis. This latter behavior will sonn be deprecated. 
        activation: Activation function of the inner states. Default: 'tanh'
        reuse: (optional) bool, whether to reuse variables in an existing scope. If not True, and the existing scope already has the given variables, an error is raised. 
    """

    super(LN_LSTMCell, self).__init__(_reuse=reuse)
    if not state_is_tuple:
      tf.logging.warn("%s: Using a concatenated state is slower and will soon be deprecated. Use state_is_tuple=True")
    self._num_units = num_units
    self._use_peepholes = use_peepholes
    self._cell_clip = cell_clip
    self._initializer = initializer
    self._num_proj = num_proj
    self._proj_clip = proj_clip
    self._forget_bias = forget_bias
    self._state_is_tuple = state_is_tuple
    self._activation = activation or math_ops.tanh

    if num_proj:
      self._state_size = (LSTMStateTuple(num_units, num_proj) if state_is_tuple else num_units + num_proj)
      self._output_size = num_proj
    else:
      self._state_size = (LSTMStateTuple(num_units, num_units) if state_is_tuple else num_units + num_units)
      self._output_size = num_units

    self._linear1 = None
    self._linear2 = None
    if self._use_peepholes:
      self._w_f_diag = None
      self._w_i_diag = None
      self._w_o_diag = None
    self._ln_i = None
    self._ln_j = None
    self._ln_f = None
    self._ln_o = None
    if self._use_peepholes:
      self._ln_p1 = None
      self._ln_p2 = None
    self._ln_c = None

  @property
  def state_size(self):
    return self._state_size
  @property
  def output_size(self):
    return self._output_size
  

  def call(self, inputs, state):
    """
      Run one step of cell,
    
      Args:
        inputs: input Tensor, 2D, batch X num_units
        state: if 'state_is_tuple' is False, this must be a state Tensor, '2D, batch X state_size'. if 'state_is_tuple' is True, this must be a tuple of state Tensors, both '2D' withcolumn sizes 'c_state' and 'm_state'
    """
    num_proj = self._num_units if self._num_proj is None else self._num_proj
    sigmoid = math_ops.sigmoid

    if self._state_is_tuple:
      (c_prev, m_prev) = state
    else:
      c_prev = array_ops.slice(state, [0, 0], [-1, self._num_units])
      m_prev = array_ops.slice(state, [0, self._num_units] , [-1, num_proj])

    dtype = inputs.dtype
    input_size = inputs.get_shape().with_rank(2)[1]
    if input_size.value is None:
      raise ValueError('Could not infer input size from inputs.get_shape()[-1]')
    if self._linear1 is None:
      scope = vs.get_variable_scope()
      with vs.variable_scope(scope, initializer=self._initializer) as unit_scope:
        self._linear1 = _Linear([inputs, m_prev], 4 * self._num_units, True)
    lstm_matrix = self._linear1([inputs, m_prev])
    # i=input_gate, j=new_input, f=forget_gate, o=output_gate
    i,j,f,o = array_ops.split(value=lstm_matrix, num_or_size_splits=4, axis=1)
    if self._ln_i is None:
      self._ln_i = Layer_Normalization([self._num_units], scope='i_norm')
    if self._ln_j is None:
      self._ln_j = Layer_Normalization([self._num_units], scope='j_norm')
    if self._ln_f is None:
      self._ln_f = Layer_Normalization([self._num_units], scope='f_norm')
    if self._ln_o is None:
      self._ln_o = Layer_Normalization([self._num_units], scope='o_norm')
    i = self._ln_i(i)
    j = self._ln_j(j)
    f = self._ln_f(f)
    o = self._ln_o(o)
    
    # diagonal connections
    if self._use_peepholes and not self._w_f_diag:
      scope = vs.get_variable_scope()
      with vs.variable_scope(scope, initializer=self._initializer) as unit_scope:
        with vs.variable_scope(unit_scope):
          self._w_f_diag = vs.get_variable("w_f_diag", shape=[self._num_units], dtype=dtype)
          self._w_i_diag = vs.get_variable("w_i_diag", shape=[self._num_units], dtype=dtype)
          self._w_o_diag = vs.get_variable("w_o_diag", shape=[self._num_units], dtype=dtype)
          if self._ln_p1 is None:
            self._ln_p1 = Layer_Normalization([self._num_units], scope='p1_norm')
          if self._ln_p2 is None:
            self._ln_p2 = Layer_Normalization([self._num_units], scope='p2_norm')
    if self._use_peepholes:
      peep1 = self._w_f_diag * c_prev
      #if self._ln_p1 is None:
      #  self._ln_p1 = Layer_Normalization([self._num_units], scope='p1_norm')
      peep2 = self._w_i_diag*c_prev
      #if self._ln_p2 is None:
      #  self._ln_p2 = Layer_Normalization([self._num_units], scope='p2_norm')
      c = (sigmoid(f + self._forget_bias + self._ln_p1(peep1)) + sigmoid(i + self._ln_p2(peep2)) * self._activation(j))
    else:
      c = (sigmoid(f + self._forget_bias) * c_prev + sigmoid(i) * self._activation(j))
    if self._ln_c is None:
      self._ln_c = Layer_Normalization([self._num_units], scope='c_norm')
    c = self._ln_c(c)
    if self._use_peepholes:
      m = sigmoid(o + self._w_o_diag * c) * self._activation(c)
    else:
      m = sigmoid(o) * self._activation(c)

    if self._num_proj is not None:
      if self._linear2 is None:
        scope = vs.get_variable_scope()
        with vs.variable_scope(scope, initializer=self._initializer):
          with vs.variable_scope("projection") as proj_scope:
            self._linear2 = _Linear(m, self._num_proj, False)
            
      m = self._linear2(m)

      if self._proj_clip is not None:
        m = clip_ops.clip_by_value(m, -self._proj_clip, self._proj_clip)
    new_state = (LSTMStateTuple(c,m) if self._state_is_tuple else array_ops.concat([c, m], 1))
    return m, new_state



class Layer_Normalization(object):

  def __init__(self, dim, scope="layer_normalization",  epsilon=1e-5):
    self._epsilon = epsilon
    with vs.variable_scope(scope) as var_scope:
      self._g = vs.get_variable('gain',dim, initializer=tf.ones_initializer(), dtype=tf.float32)
      self._b = vs.get_variable('bias',dim, initializer=tf.zeros_initializer(), dtype=tf.float32)

  def __call__(self, inputs):
      m, v = tf.nn.moments(inputs, [1], keep_dims=True)
      norm_inputs = (inputs - m) / tf.sqrt(v + self._epsilon)
      return norm_inputs * self._g + self._b
    
def orthogonal(shape):
  flat_shape = (shape[0], np.prod(shape[1:]))
  a = np.random.normal(0.0, 1.0, flat_shape)
  u, _, v = np.linalg.svd(a, full_matrices=False)
  q = u if u.shape == flat_shape else v
  return q.reshape(shape)

def lstm_ortho_initializer(scale=1.0):
  def _initializer(shape, dtype=tf.float32, partition_info=None):
    size_x = shape[0]
    size_h = shape[1]/4
    t = np.zeros(shape)
    t[:, :size_h] = orthogonal([size_x, size_h]) * scale
    t[:, size_h: size_h*2] = orthogonal([size_x, size_h]) * scale
    t[:, size_h*2: size_h*3] = orthogonal([size_x, size_h]) * scale
    t[:, size_h*3: ] = orthogonal([size_x, size_h] * scale)
    return tf.constant(t,dtype)
  return _initializer



def _h_linear(x, output_size, scope=None, reuse=False,
              init_w="ortho", weight_start=0.0, use_bias=True, bias_start=0.0, input_size=None):
  shape = x.get_shape().as_list()
  with tf.variable_scope(scope or 'linear'):
    if reuse == True:
      tf.get_variable_scope().reuse_variables()
    w_init = None
    if input_size == None:
      x_size = shape[1]
    else:
      x_size = output_size
    if init_w =='zeros':
      w_init = tf.constant_initializer(0.0)
    elif init_w == 'constant':
      w_init = tf.constant_initializer(weight_start)
    elif init_w == 'gaussian':
      w_init = tf.random_normal_initializer(stddev=weight_start)
    elif init_w == 'ortho':
      w_init=lstm_ortho_initializer(1.0)
    w = tf.get_variable('super_linear_w', [x_size, output_size], tf.float32, initializer=w_init)
    if use_bias:
      b = tf.get_variable('super_linear_b', [output_size], tf.float32, initializer=tf.constant_initializer(bias_start))
      return tf.matmul(x,w) + b
    return tf.matmul(x,w)

def _hyper_norm(layer, hyper_output, embedding_size, num_units, scope="hyper", use_bias=True):
  init_gamma = 0.10
  with tf.variable_scope(scope):
    zw = _h_linear(hyper_output, embedding_size, init_w="constant", weight_start=0.0, use_bias=True, bias_start=1.0, scope='zw')
    alpha = _h_linear(zw, num_units, init_w="constant", weight_start=init_gamma/embedding_size, use_bias=False, scope='alpha')
    return tf.multiply(alpha, layer)


def _hyper_bias(layer, hyper_output, embedding_size, num_units, scope="hyper"):
  with tf.variable_scope(scope):
    zb = _h_linear(hyper_output, embedding_size, init_w='gaussian', weight_start=0.01, use_bias=False, bias_start=0.0, scope='zb')
    beta = _h_linear(zb, num_units, init_w='constant', weight_start=0.0, use_bias=False, scope='beta')
  return layer + beta



class H_LSTMCell(RNNCell):
  """
    Extensrion of LSTM recurrent network cell to 
    Hyper network LSTM cell
    https://arxiv.org/abs/1609.09106
  """
  def __init__(self, num_units,
               use_peepholes=False,
               cell_clip=None,
               initializer=None,
               num_proj=None,
               proj_clip=None,
               forget_bias=1.0,
               #state_is_tuple = True,
               activation=None,
               reuse=None,
               hyper_num_units=32,
               hyper_embed_size=16):
    """
      num_units: int, number of units in cell.
      use_peepholes: bool, true to enable diagonal/peephole connections.
      cell_clip: (optional) float, if provided the cell state is clipped by this value prior to the cell output activation.
      initializer: (optional) , initializer to use for the weight and projection matrics.
      num_proj: (optional) int, output dimensionality for the projection matrics, if none, no projection is performed.
      proj_clip: optional() float, if 'num_proj'>0 and 'proj_clip' is provided, then the projected values are clipped elementwise to within '[-proj_clip, proj_clip]'
      forget_bias: float, bias of the forget gate are initialized by default to 1.0 in order to reduce the scale of forgetting at the begining of the training. Must set it manually to '0.0' when restoring CudnnLSTM trained checkpoint.
      remove state_is_tuple: bool, if True, accepted and returned states are 2-tuples of the 'c_state' and 'm_state'. If False, they are concatenated along the column axis. This latter behavior will soon be deprecated.
      activation: activation function of the inner states. Default 'tanh'
      reuse: (optional) bool, whether to reuse variables in an existing scope, If not True, and the existing scope already has the given variables, an error is raised.
    """
    super(H_LSTMCell, self).__init__(_reuse=reuse)
    #if not state_is_tuple:
    #  tf.logging.warn('%s: Using a concatenated state is slower and will soon be deprecated, Use state_is_tuple=True')
    self._num_units = num_units
    self._use_peepholes = use_peepholes
    self._cell_clip = cell_clip
    self._initializer = initializer
    self._num_proj = num_proj
    self._proj_clip = proj_clip
    self._forget_bias = forget_bias
    #self._state_is_tuple = state_is_tuple
    self._activation = activation or math_ops.tanh
    self._hyper_num_units = hyper_num_units
    self._hyper_embed_size = hyper_embed_size


    if num_proj:
      self._state_size = (LSTMStateTuple(num_units+hyper_num_units, num_proj+hyper_num_units))
      self._output_size = num_proj
    else:
      self._state_size = (LSTMStateTuple(num_units+hyper_num_units, num_units+hyper_num_units))
      self._output_size = num_units


    #self._w_h_linear = None
    if self._use_peepholes:
      self._w_f_diag = None
      self._w_i_diag = None
      self._w_o_diag = None
    self._ln_i = None
    self._ln_j = None
    self._ln_f = None
    self._ln_o = None
    if self._use_peepholes:
      self._ln_p1 = None
      self._ln_p2 = None
    self._ln_c = None


    self._hyper_cell = None

  @property
  def state_size(self):
    return self._state_size
  @property
  def output_size(self):
    return self._output_size

  def call(self, inputs, state):
    """
      run one step of cell
      Args:
        inputs: input tensor, 2D, batch X num_units
        state: if 'state_is_tuple' is False, this must be a state Tensor, '2D', batch x state_size. if 'state_is_tuple' is True, this must be a tuple of state Tensors, both '2D' with column size 'c_state' and 'm_state'
    """
    num_proj = self._num_units if self._num_proj is None else self._num_proj
    sigmoid = math_ops.sigmoid

    c_t, m_t= state
    c_prev, m_prev = c_t[:, 0:self._num_units], m_t[:, 0:self._num_units]
    hyper_state = LSTMStateTuple(c_t[:,self._num_units:], m_t[:,self._num_units:])

    w_init = None
    h_init = lstm_ortho_initializer(1.0)


    #if True: # self._state_is_tuple:
      #(c_prev, m_prev) = state
    #else:
    #  c_prev = array_ops.slice(state, [0,0], [-1,self._num_units])
    #  m_prev = array_ops.slice(state, [0,self._num_units], [-1,num_proj])
    dtype = inputs.dtype
    input_size = inputs.get_shape().with_rank(2)[1]

    if input_size.value is None:
      raise ValueError('Could not infer input size from inputs.get_shape()[-1]')

    batch_size = inputs.get_shape().with_rank(2)[0]
    #print(inputs)
    x_hat = tf.concat([inputs , m_prev],1)
    #print(x_hat)

    if self._hyper_cell is None:
      with vs.variable_scope('hyper_lstm') as scope:
        self._hyper_cell = LSTMCell(self._hyper_num_units)
    h_out, new_hyper_state = self._hyper_cell(x_hat, hyper_state)

    W_xh = tf.get_variable('W_xh', [input_size, self._num_units*4], initializer=w_init)
    W_hh = tf.get_variable('W_hh', [input_size, self._num_units*4], initializer=w_init)
    bias = tf.get_variable('W_bias', [self._num_units*4], initializer=tf.constant_initializer(0.0))

    xh = tf.matmul(inputs, W_xh)
    hh = tf.matmul(inputs, W_hh)

    ix, jx, fx, ox = tf.split(xh, 4, 1)
    ix = _hyper_norm(ix, h_out, self._hyper_embed_size, self._num_units, 'hyper_ix')
    jx = _hyper_norm(jx, h_out, self._hyper_embed_size, self._num_units, 'hyper_jx')
    fx = _hyper_norm(fx, h_out, self._hyper_embed_size, self._num_units, 'hyper_fx')
    ox = _hyper_norm(ox, h_out, self._hyper_embed_size, self._num_units, 'hyper_ox')


    ih, jh, fh, oh = tf.split(hh, 4, 1)
    ih = _hyper_norm(ih, h_out, self._hyper_embed_size, self._num_units, 'hyper_ih')
    jh = _hyper_norm(jh, h_out, self._hyper_embed_size, self._num_units, 'hyper_jh')
    fh = _hyper_norm(fh, h_out, self._hyper_embed_size, self._num_units, 'hyper_fh')
    oh = _hyper_norm(oh, h_out, self._hyper_embed_size, self._num_units, 'hyper_oh')

    
    ib, jb, fb, ob = tf.split(bias, 4, 0)
    ib = _hyper_bias(ib, h_out, self._hyper_embed_size, self._num_units, 'hyper_ib')
    jb = _hyper_bias(jb, h_out, self._hyper_embed_size, self._num_units, 'hyper_jb')
    fb = _hyper_bias(fb, h_out, self._hyper_embed_size, self._num_units, 'hyper_fb')
    ob = _hyper_bias(ob, h_out, self._hyper_embed_size, self._num_units, 'hyper_ob')

    i = ix + ih + ib
    j = jx + jh + jb
    f = fx + fh + fb
    o = ox + oh + ob
    print(i)

    #if self._w_h_linear is None:
    #  with vs.variable_scope('w_h_linear') as scope:
    #    self._w_h_linear = _Linear([])



    #if self._linear1 is None:
    #  scope = vs.get_variable_scope()
    #  with vs.variable_scope(scope, initializer=self._initializer) as unit_scope:
    #    self._linear1 = _Linear([inputs, m_prev], 4*self._num_units, True)
    #lstm_matrix = self._linear1([inputs, m_prev])
    #i,j,f,o = array_ops.split(value=lstm_matrix, num_or_size_splits=4, axis=1)
    if self._ln_i is None:
      self._ln_i = Layer_Normalization([self._num_units], scope='i_norm')
    if self._ln_j is None:
      self._ln_j = Layer_Normalization([self._num_units], scope='j_norm')
    if self._ln_f is None:
      self._ln_f = Layer_Normalization([self._num_units], scope='f_norm')
    if self._ln_o is None:
      self._ln_o = Layer_Normalization([self._num_units], scope='o_norm')
    i,j,f,o = self._ln_i(i), self._ln_j(j), self._ln_f(f), self._ln_o(o)

    if self._use_peepholes and not self._w_f_diag:
      scope = vs.get_variable_scope()
      with vs.variable_scope(scope, initializer=self._initializer) as unit_scope:
        with vs.variable_scope(unit_scope):
          self._w_f_diag = vs.get_variable("w_f_diag", shape=[self._num_units], dtype=dtype)
          self._w_i_diag = vs.get_variable("w_i_diag", shape=[self._num_units], dtype=dtype)
          self._w_o_diag = vs.get_variable("w_o_diag", shape=[self._num_units], dtype=dtype)
          if self._ln_p1 is None:
            self._ln_p1 = Layer_Normalization([self._num_units], scope='p1_norm')
          if self._ln_p2 is None:
            self._ln_p2 = Layer_Normalization([self._num_units], scope='p2_norm')
    if self._use_peepholes:
      peep1 = self._w_f_diag * c_prev
      peep2 = self._w_i_diag * c_prev
      c = (sigmoid(f + self._forget_bias + self._ln_p1(peep1)) + sigmoid(i + self._ln_p2(peep2)) * self._activation(j))
    else:
      c = sigmoid(f + self._forget_bias) * c_prev + sigmoid(i) * self._activation(j)
    if self._ln_c is None:
      self._ln_c = Layer_Normalization([self._num_units], scope='c_norm')
    c = self._ln_c(c)
    if self._use_peepholes:
      m = sigmoid(o + self._w_o_diag * c) * self._activation(c)
    else:
      m = sigmoid(o) * self._activation(c)

    if self._num_proj is not None:
      if self._linear is None:
        scope = vs.get_variable_scope(scope)
        with vs.variable_scope(scope, initializer=self._initializer):
          with vs.variable_scope("projection") as proj_scope:
            self._linear2 = _Linear(m, self._num_proj, False)
      m = self._linear2(m)
      if self._proj_clip is not None:
        m = clip_ops.clip_by_value(m, -self._proj_clip, self._proj_clip)
    hyper_c, hyper_h = new_hyper_state
    new_state = LSTMStateTuple(tf.concat([c,hyper_c],1),tf.concat([m,hyper_h],1))
    return m, new_state


class _Linear(object):
  """Linear map: sum_i(args[i] * W[i]), where W[i] is a variable.

  Args:
    args: a 2D Tensor or a list of 2D, batch x n, Tensors.
    output_size: int, second dimension of weight variable.
    dtype: data type for variables.
    build_bias: boolean, whether to build a bias variable.
    bias_initializer: starting value to initialize the bias
      (default is all zeros).
    kernel_initializer: starting value to initialize the weight.

  Raises:
    ValueError: if inputs_shape is wrong.
  """

  def __init__(self,
               args,
                     output_size,
               build_bias,
               bias_initializer=None,
               kernel_initializer=None):
    self._build_bias = build_bias

    if args is None or (nest.is_sequence(args) and not args):
      raise ValueError("`args` must be specified")
    if not nest.is_sequence(args):
      args = [args]
      self._is_sequence = False
    else:
      self._is_sequence = True

    # Calculate the total size of arguments on dimension 1.
    total_arg_size = 0
    shapes = [a.get_shape() for a in args]
    for shape in shapes:
      if shape.ndims != 2:
        raise ValueError("linear is expecting 2D arguments: %s" % shapes)
      if shape[1].value is None:
        raise ValueError("linear expects shape[1] to be provided for shape %s, "
                         "but saw %s" % (shape, shape[1]))
      else:
        total_arg_size += shape[1].value

    dtype = [a.dtype for a in args][0]

    scope = vs.get_variable_scope()
    with vs.variable_scope(scope) as outer_scope:
      self._weights = vs.get_variable(
          _WEIGHTS_VARIABLE_NAME, [total_arg_size, output_size],
          dtype=dtype,
          initializer=kernel_initializer)
      if build_bias:
        with vs.variable_scope(outer_scope) as inner_scope:
          inner_scope.set_partitioner(None)
          if bias_initializer is None:
            bias_initializer = init_ops.constant_initializer(0.0, dtype=dtype)
          self._biases = vs.get_variable(
              _BIAS_VARIABLE_NAME, [output_size],
              dtype=dtype,
              initializer=bias_initializer)

  def __call__(self, args):
    if not self._is_sequence:
      args = [args]

    if len(args) == 1:
      res = math_ops.matmul(args[0], self._weights)
    else:
      res = math_ops.matmul(array_ops.concat(args, 1), self._weights)
    if self._build_bias:
      res = nn_ops.bias_add(res, self._biases)
    return res




if __name__ == '__main__':
  print('testing cell')
  #cell1 = Hyper_LSTMCell(5, use_peepholes=True)
  #with vs.variable_scope('c1') as scope:
  #  cell1 = LSTMCell(5, use_peepholes=True)
  #with vs.variable_scope('c2') as scope:
  #  cell2 = LSTMCell(5, use_peepholes=True)
  c = tf.get_variable('c',[3,15])
  m = tf.get_variable('m',[3,15])
  i = tf.get_variable('i',[3,15])
  t = LSTMStateTuple(c,m)
  with vs.variable_scope('c2') as scope:
    cell2 = H_LSTMCell(10, use_peepholes=True, hyper_num_units=5)
    o2 = cell2(i,t)
  sess = tf.Session()
  sess.run(tf.global_variables_initializer())
  a1,a2,a3 = sess.run([o2,t,i])
  print(a1)
  print('____________')
  print(a2)
  print('____________')
  print(a3)
  #print(r1)
  #print('-----------')
  #print(r2)
