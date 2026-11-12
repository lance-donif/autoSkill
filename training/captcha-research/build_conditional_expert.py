"""Build the one predeclared scalar-If old-specialist research prototype."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper, version_converter


def sha(file):
    """Content address source assets before any model transformation."""
    return hashlib.sha256(file.read_bytes()).hexdigest()


def load_helper(file):
    """Load a previously frozen research helper without editing its source."""
    spec = importlib.util.spec_from_file_location(file.stem, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tensor_payload(tensor):
    """Compare initializer bytes while allowing local subgraph name prefixes."""
    original = copy.deepcopy(tensor)
    original.name = ''
    return original.SerializeToString()


def verify_assets(protocol):
    """Check every fixed file used by this conversion, never a moving baseline."""
    pairs = [('source_model', 'source_sha256'), ('source_charset', 'source_charset_sha256'),
             ('base_model', 'base_sha256'), ('base_metadata', 'base_metadata_sha256'),
             ('gate_helper', 'gate_helper_sha256'), ('gate_numpy_helper', 'gate_numpy_helper_sha256')]
    for file, expected in pairs:
        if sha(Path(protocol[file])) != protocol[expected]:
            raise ValueError('Frozen asset changed: ' + file)


def gray_resize_nodes():
    """Crop, integer-gray, bilinear-resize and legacy-normalize inside then only."""
    nodes, constants = [], []
    prefix = 'expert_adapter/'

    def constant(name, value, dtype=np.float32):
        key = prefix + name
        constants.append(numpy_helper.from_array(np.asarray(value, dtype=dtype), key))
        return key

    def node(op, inputs, name, **attributes):
        key = prefix + name
        nodes.append(helper.make_node(op, inputs, [key], name=key, **attributes))
        return key

    width = node('Cast', ['physical_sparse_line/valid_count'], 'valid_width', to=TensorProto.INT64)
    crop = node('Slice', ['physical_sparse_line/raw_integer', constant('start', [0], np.int64), width,
                          constant('width_axis', [3], np.int64)], 'cropped_bgr')
    coefficients = constant('pil_bgr_gray_coefficients', np.asarray([7471, 38470, 19595]).reshape(1, 3, 1, 1))
    weighted = node('Mul', [crop, coefficients], 'weighted_bgr')
    total = node('ReduceSum', [weighted, constant('channel_axis', [1], np.int64)], 'gray_numerator', keepdims=1)
    gray = node('Floor', [node('Div', [node('Add', [total, constant('gray_round_bias', 32768)], 'gray_biased'),
                                      constant('gray_divisor', 65536)], 'gray_quotient')], 'gray_integer')
    expanded_width = node('Mul', [width, constant('height64', [64], np.int64)], 'expanded_width')
    target_width = node('Max', [node('Div', [expanded_width, constant('height48', [48], np.int64)], 'floor_width'),
                              constant('minimum_width', [1], np.int64)], 'target_width')
    sizes = node('Concat', [constant('nch_prefix', [1, 1, 64], np.int64), target_width], 'resize_sizes', axis=0)
    resized = node('Resize', [gray, constant('empty_roi', []), constant('empty_scales', []), sizes], 'bilinear_gray',
                   mode='linear', coordinate_transformation_mode='half_pixel')
    normalized = node('Sub', [node('Div', [resized, constant('normalization_scale', 127.5)], 'scaled_gray'),
                              constant('normalization_bias', 1)], 'normalized')
    return nodes, constants, gray, normalized


def mapping_nodes(raw, source_charset, target_charset):
    """Preserve the stable first-index source argmax and filter unsupported tokens."""
    target = {char: index for index, char in enumerate(target_charset)}
    lookup = np.asarray([target.get(char, 0) for char in source_charset], np.int64)
    nodes = [helper.make_node('ArgMax', [raw], ['expert_adapter/source_indices'],
                              name='expert_adapter/first_argmax', axis=2, keepdims=0, select_last_index=0),
             helper.make_node('Gather', ['expert_adapter/class_lookup', 'expert_adapter/source_indices'],
                              ['expert_adapter/target_indices_tn'], name='expert_adapter/class_lookup_gather', axis=0),
             helper.make_node('Transpose', ['expert_adapter/target_indices_tn'], ['expert_adapter/target_indices_nt'],
                              name='expert_adapter/batch_time', perm=[1, 0]),
             helper.make_node('OneHot', ['expert_adapter/target_indices_nt', 'expert_adapter/class_count', 'expert_adapter/onehot_values'],
                              ['expert_adapter/output'], name='expert_adapter/onehot_decoder_interface', axis=-1)]
    constants = [numpy_helper.from_array(lookup, 'expert_adapter/class_lookup'),
                 numpy_helper.from_array(np.asarray(63, np.int64), 'expert_adapter/class_count'),
                 numpy_helper.from_array(np.asarray([0., 1.], np.float32), 'expert_adapter/onehot_values')]
    return nodes, constants, lookup


def converted_expert(source):
    """Repair only stale output annotation, then convert builtin operator versions."""
    model = onnx.load(source)
    original = {item.name: tensor_payload(item) for item in model.graph.initializer}
    output_name = model.graph.output[0].name
    del model.graph.output[:]
    model.graph.output.extend([helper.make_tensor_value_info(output_name, TensorProto.FLOAT, ['expert_time', 1, 8210])])
    model = version_converter.convert_version(model, 17)
    converted = {item.name: tensor_payload(item) for item in model.graph.initializer}
    if any(converted.get(name) != data for name, data in original.items()):
        raise ValueError('Version conversion changed an original expert initializer')
    return model, original


def expert_branch(model, source_charset, target_charset):
    """Move all expert arithmetic inside the If true branch, after its adapter."""
    preprocessing, constants, _, normalized = gray_resize_nodes()
    source_input = model.graph.input[0].name
    names = {name for node in model.graph.node for name in [*node.input, *node.output] if name}
    names.update(item.name for item in model.graph.initializer)
    rename = {name: 'expert/' + name for name in names}
    rename[source_input] = normalized
    nodes = copy.deepcopy(list(model.graph.node))
    for index, node in enumerate(nodes):
        node.name = 'expert/' + (node.name or f'{node.op_type}_{index}')
        for slot, name in enumerate(node.input):
            if name:
                node.input[slot] = rename[name]
        for slot, name in enumerate(node.output):
            node.output[slot] = rename[name]
    weights = copy.deepcopy(list(model.graph.initializer))
    for tensor in weights:
        tensor.name = rename[tensor.name]
    raw = rename[model.graph.output[0].name]
    mapped, mapping_constants, lookup = mapping_nodes(raw, source_charset, target_charset)
    graph = helper.make_graph([*preprocessing, *nodes, *mapped], 'old_specialist_then', [],
                              [helper.make_tensor_value_info('expert_adapter/output', TensorProto.FLOAT, [1, 'expert_time', 63])],
                              [*constants, *weights, *mapping_constants])
    return graph, lookup


def base_branch(base):
    """Keep original base initializer bytes, with node labels for profiling only."""
    nodes = copy.deepcopy(list(base.graph.node))
    for index, node in enumerate(nodes):
        node.name = 'base/' + (node.name or f'{node.op_type}_{index}')
    return helper.make_graph(nodes, 'b583_else', [], copy.deepcopy(list(base.graph.output)),
                              copy.deepcopy(list(base.graph.initializer)))


def adapter_model(input_name, gate):
    """Expose adapted gray inputs and the fixed gate for independent dummy checks."""
    condition, constants, flag, _ = gate.condition_nodes(input_name)
    preprocessing, adapter_constants, gray, normalized = gray_resize_nodes()
    graph = helper.make_graph([*condition, *preprocessing], 'expert_adapter_diagnostic',
                              [helper.make_tensor_value_info(input_name, TensorProto.FLOAT, [1, 3, 48, 320])],
                              [helper.make_tensor_value_info(normalized, TensorProto.FLOAT, [1, 1, 64, None]),
                               helper.make_tensor_value_info(gray, TensorProto.FLOAT, [1, 1, 48, None]),
                               helper.make_tensor_value_info(flag, TensorProto.BOOL, [1])], [*constants, *adapter_constants])
    result = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)], producer_name='autoskill-research')
    result.ir_version = 8
    onnx.checker.check_model(result)
    return result


def build(protocol, output):
    """Write one research-only wrapper and standalone adapter diagnostic graphs."""
    verify_assets(protocol)
    if output.exists():
        raise ValueError('Use a new build directory')
    output.mkdir(parents=True)
    gate = load_helper(Path(protocol['gate_helper']))
    base = onnx.load(protocol['base_model'])
    source_charset = json.loads(Path(protocol['source_charset']).read_text())
    target_charset = json.loads(Path(protocol['base_metadata']).read_text())['charset']
    if len(source_charset) != 8210 or len(set(source_charset)) != 8210 or any(source_charset.count(char) != 1 for char in target_charset):
        raise ValueError('Source charset cannot uniquely cover the entire target charset')
    expert, original_weights = converted_expert(Path(protocol['source_model']))
    onnx.save(expert, output / 'converted-source.onnx')
    then, lookup = expert_branch(expert, source_charset, target_charset)
    otherwise = base_branch(base)
    onnx.save(adapter_model(base.graph.input[0].name, gate), output / 'adapter-only.onnx')
    nodes, constants, flag, _ = gate.condition_nodes(base.graph.input[0].name)
    constants.append(numpy_helper.from_array(np.asarray([0], np.int64), 'expert_routing/scalar_axis'))
    scalar = helper.make_node('Squeeze', [flag, 'expert_routing/scalar_axis'], ['expert_routing/condition'], name='expert_routing/scalar_condition')
    conditional = helper.make_node('If', ['expert_routing/condition'], ['recognition/trained_probabilities'],
                                   name='expert_routing/if', then_branch=then, else_branch=otherwise)
    input_value = copy.deepcopy(base.graph.input[0])
    input_value.type.tensor_type.shape.dim[0].Clear()
    input_value.type.tensor_type.shape.dim[0].dim_value = 1
    graph = helper.make_graph([*nodes, scalar, conditional], 'conditional_captcha_specialist_v1', [input_value],
                              [helper.make_tensor_value_info('recognition/trained_probabilities', TensorProto.FLOAT, [1, 'ctc_time', 63])], constants)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17), helper.make_opsetid('com.microsoft', 1)], producer_name='autoskill-research')
    model.ir_version = 8
    model.doc_string = 'Research-only fixed v4.1 physical routing; original old expert argmax mapped to onehot63, not confidence.'
    onnx.checker.check_model(model)
    candidate = output / 'conditional-expert.onnx'
    onnx.save(model, candidate)
    before = {item.name: item.SerializeToString() for item in base.graph.initializer}
    after = {item.name: item.SerializeToString() for item in otherwise.initializer}
    if before != after:
        raise ValueError('The base branch changed a B583 initializer')
    embedded = {item.name.removeprefix('expert/'): tensor_payload(item) for item in then.initializer if item.name.startswith('expert/')}
    if any(embedded.get(name) != data for name, data in original_weights.items()):
        raise ValueError('The If branch changed an original expert initializer')
    report = {'status': 'BUILT_NOT_YET_EVALUATED', 'candidate': str(candidate), 'candidate_sha256': sha(candidate),
              'source_initializers_unchanged': len(original_weights), 'base_initializers_unchanged': len(before),
              'target_class_count': len(target_charset), 'source_class_count': len(source_charset),
              'mapped_alnum_classes': int(np.count_nonzero(lookup)), 'unsupported_and_blank_indices': int(np.count_nonzero(lookup == 0)),
              'output_shape': [1, None, 63], 'adapter_ops_standard': True, 'retained_source_custom_domain': 'com.microsoft::DynamicQuantizeLSTM',
              'model_bytes': candidate.stat().st_size, 'builder_sha256': sha(Path(__file__))}
    (output / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(json.loads(args.protocol.read_text()), args.output), indent=2))  # noqa: T201  # research build report
