// The TF.js converter fused BiasAdd + ReLU, hiding the pre-activation embedding
// used to train Essentia's mood heads. Restore a parallel BiasAdd-only readout
// using the exact same input, weights and bias. Leave the native tag path intact.
export function embeddingGraph(graph) {
  const nodes = graph.modelTopology.node;
  if (nodes.some(node => node.name === 'model/dense/BiasAdd')) return graph;
  const fused = nodes.find(node => node.name === 'model/dense/Relu');
  if (fused?.op !== '_FusedMatMul' || fused.input.length !== 3
      || JSON.stringify(fused.attr.fused_ops.list.s) !== JSON.stringify(['Qmlhc0FkZA==', 'UmVsdQ=='])
      || fused.input[1] !== 'dense/kernel' || fused.input[2] !== 'dense/bias')
    throw Error('Unsupported MusiCNN embedding graph');
  const readout = JSON.parse(JSON.stringify(fused));
  readout.name = 'model/dense/BiasAdd';
  readout.attr.fused_ops.list.s = ['Qmlhc0FkZA=='];
  return {...graph, modelTopology: {...graph.modelTopology, node: [...nodes, readout]}};
}
