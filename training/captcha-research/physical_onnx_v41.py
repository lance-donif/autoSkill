"""Build a portable ONNX form of the fixed sparse through-line pixel condition."""
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def condition_nodes(input_name):
    """Use FP32 Round, valid-image widths and sparse near-gray coverage only."""
    nodes, constants = [], []
    prefix = 'physical_sparse_line/'

    def constant(name, value, dtype=np.float32):
        key = prefix + name
        constants.append(numpy_helper.from_array(np.asarray(value, dtype=dtype), key))
        return key

    def node(op, inputs, name, **attributes):
        key = prefix + name
        nodes.append(helper.make_node(op, inputs, [key], name=key, **attributes))
        return key

    zero, one = constant('zero', 0), constant('one', 1)
    absolute = node('Abs', [input_name], 'absolute_input')
    magnitude = node('ReduceMax', [absolute], 'valid_column_magnitude', axes=[1, 2], keepdims=0)
    valid = node('Greater', [magnitude, zero], 'valid_columns')
    raw = node('Round', [node('Mul', [node('Add', [input_name, one], 'shifted_input'), constant('raw_scale', 127.5)], 'scaled_input')], 'raw_integer')
    maximum = node('ReduceMax', [raw], 'max_rgb', axes=[1], keepdims=0)
    minimum = node('ReduceMin', [raw], 'min_rgb', axes=[1], keepdims=0)
    dark = node('Less', [maximum, constant('max_rgb_exclusive', 128)], 'near_black')
    spread = node('Sub', [maximum, minimum], 'rgb_spread')
    gray = node('LessOrEqual', [spread, constant('rgb_spread_maximum', 16)], 'near_gray')
    pixels = node('And', [dark, gray], 'dark_gray_pixels')
    valid_broadcast = node('Unsqueeze', [valid, constant('pixel_broadcast_axes', [1], np.int64)], 'valid_pixel_columns')
    valid_pixels = node('And', [pixels, valid_broadcast], 'valid_dark_gray_pixels')
    pixel_float = node('Cast', [valid_pixels], 'dark_gray_float', to=TensorProto.FLOAT)
    column_maximum = node('ReduceMax', [pixel_float], 'dark_gray_column_maximum', axes=[1], keepdims=0)
    covered = node('Greater', [column_maximum, zero], 'covered_columns')
    count_axis = constant('column_count_axis', [1], np.int64)
    valid_count = node('ReduceSum', [node('Cast', [valid], 'valid_column_float', to=TensorProto.FLOAT), count_axis], 'valid_count', keepdims=0)
    covered_count = node('ReduceSum', [node('Cast', [covered], 'covered_column_float', to=TensorProto.FLOAT), count_axis], 'covered_count', keepdims=0)
    coverage = node('Div', [covered_count, node('Max', [valid_count, one], 'safe_valid_count')], 'coverage')
    dark_count = node('ReduceSum', [pixel_float, constant('pixel_count_axes', [1, 2], np.int64)], 'dark_pixel_count', keepdims=0)
    shape = node('Shape', [input_name], 'input_shape')
    height = node('Cast', [node('Gather', [shape, constant('height_index', [2], np.int64)], 'height_integer', axis=0)], 'height', to=TensorProto.FLOAT)
    pixel_count = node('Mul', [valid_count, height], 'valid_pixel_count')
    fraction = node('Div', [dark_count, node('Max', [pixel_count, one], 'safe_pixel_count')], 'dark_pixel_fraction')
    full = node('GreaterOrEqual', [coverage, constant('coverage_minimum', .90)], 'full_width')
    sparse = node('LessOrEqual', [fraction, constant('dark_pixel_fraction_maximum', .50)], 'sparse')
    nonempty = node('Greater', [valid_count, zero], 'nonempty_image')
    flag = node('And', [node('And', [full, sparse], 'full_sparse'), nonempty], 'flag')
    mask = node('Unsqueeze', [node('Cast', [flag], 'flag_float', to=TensorProto.FLOAT), constant('residual_broadcast_axes', [1, 2], np.int64)], 'residual_mask')
    return nodes, constants, flag, mask


def condition_model():
    """Expose only the condition for cheap exact bool and boundary verification."""
    nodes, constants, flag, mask = condition_nodes('image')
    graph = helper.make_graph(nodes, 'sparse-through-line-condition-v4.1',
                              [helper.make_tensor_value_info('image', TensorProto.FLOAT, [None, 3, None, None])],
                              [helper.make_tensor_value_info(flag, TensorProto.BOOL, [None]),
                               helper.make_tensor_value_info(mask, TensorProto.FLOAT, [None, 1, 1])], constants)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)], producer_name='autoskill-research')
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


def attach_condition(model):
    """Multiply the complete already-bounded residual, leaving base weights alone."""
    nodes, constants, _, mask = condition_nodes(model.graph.input[0].name)
    old = list(model.graph.node)
    index = next(i for i, item in enumerate(old) if list(item.output) == ['temporal_residual/scaled'])
    if old[index].input[0] != 'temporal_residual/residual':
        raise ValueError('The adapter stage is not the expected bounded full residual')
    conditioned = 'physical_sparse_line/conditioned_residual'
    multiplier = helper.make_node('Mul', ['temporal_residual/residual', mask], [conditioned], name=conditioned)
    old[index].input[0] = conditioned
    del model.graph.node[:]
    model.graph.node.extend([*nodes, *old[:index], multiplier, *old[index:]])
    model.graph.initializer.extend(constants)
    model.doc_string += '\nFixed sparse near-black through-line condition v4.1 after cap*tanh; unchanged alpha=1.'
    onnx.checker.check_model(model)
    return model


def example_tensor(raw):
    """Create byte-exact product-scaled boundary inputs, padded to width 320."""
    height, width, _ = raw.shape
    tensor = np.zeros((1, 3, height, 320), np.float32)
    tensor[0, :, :, :width] = (raw.astype(np.float32)[:, :, ::-1] / np.float32(127.5) - np.float32(1)).transpose(2, 0, 1)
    return tensor


def condition_examples():
    """Cover semantic negatives and every inclusive/exclusive fixed boundary."""
    examples = []
    for name, color, expected in [('thin-black', (30, 30, 30), True), ('colored-line', (20, 100, 20), False),
                                   ('max127', (127, 127, 127), True), ('max128', (128, 128, 128), False),
                                   ('range16', (111, 127, 127), True), ('range17', (110, 127, 127), False)]:
        raw = np.full((48, 100, 3), 255, np.uint8)
        raw[20] = color
        examples.append((name, example_tensor(raw), expected))
    for name, start, end, expected in [('coverage90', 5, 95, True), ('coverage89', 5, 94, False), ('local-glyph', 20, 70, False)]:
        raw = np.full((48, 100, 3), 255, np.uint8)
        raw[20, start:end] = 30
        examples.append((name, example_tensor(raw), expected))
    for rows, expected in [(24, True), (25, False), (48, False)]:
        raw = np.full((48, 100, 3), 255, np.uint8)
        raw[:rows] = 30
        examples.append((f'dark-rows-{rows}', example_tensor(raw), expected))
    raw = np.zeros((48, 100, 3), np.uint8)
    examples.append(('all-black', example_tensor(raw), False))
    raw[10:36, 20:70] = 255
    examples.append(('white-glyphs-dark-background', example_tensor(raw), False))
    examples.append(('all-zero-invalid-tensor', np.zeros((1, 3, 48, 320), np.float32), False))
    return examples
