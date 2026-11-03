export interface CorpusDoc {
  id: string;
  title: string;
  text: string;
  source: string;
}

export interface EvalQuery {
  query: string;
  relevant: string[];
}

/** Fixed corpus used by the demo import and the quantization / performance benchmark. */
export const CORPUS: CorpusDoc[] = [
  {
    id: 'b01',
    title: 'seekdb 是什么',
    source: 'seekdb 概览',
    text: 'seekdb 是 OceanBase 推出的轻量向量数据库，同时提供向量索引与全文索引。它既可以作为本地嵌入式数据库随应用一起运行，也可以连接本地 server，适合个人知识库、桌面工具和端侧 AI 应用。',
  },
  {
    id: 'b02',
    title: 'seekdb 混合检索',
    source: 'seekdb 检索',
    text: 'seekdb 通过 DBMS_HYBRID_SEARCH 在数据库内部完成向量检索与全文检索，并使用 RRF 对两路候选结果融合排序。客户端只需提交查询向量和查询文本，不需要自己拉取两路结果后再拼合。',
  },
  {
    id: 'b03',
    title: 'HNSW 向量索引',
    source: 'seekdb 索引',
    text: 'HNSW 是一种分层可导航小世界图索引，用少量跳转即可在向量集合中找到近似的最近邻。seekdb 的向量索引支持 HNSW，并可以配置距离度量、构建参数和检索参数，从而在召回率与延迟之间取得平衡。',
  },
  {
    id: 'b04',
    title: '全文索引与 ngram 分词',
    source: 'seekdb 索引',
    text: 'seekdb 全文索引支持 ngram、ngram2、beng、ik 等分词器。对中文资料来说，ngram2 会把文本切成 2 到 3 个字符的片段，使连续中文也能被关键词检索命中，而不依赖空格分词。',
  },
  {
    id: 'b05',
    title: '嵌入式部署',
    source: 'seekdb 部署',
    text: '嵌入式模式只需要给出一个本地数据目录，数据库随 Node 进程启动，不需要额外安装数据库服务。数据落在本地文件中，离线环境也能使用，非常适合演示、单机工具和小规模知识库。',
  },
  {
    id: 'b06',
    title: 'OceanBase 分布式数据库',
    source: 'OceanBase',
    text: 'OceanBase 是分布式关系数据库，强调高可用、水平扩展和事务一致性。seekdb 复用了 OceanBase 的 SQL 与存储能力，但面向端侧和轻量场景做了裁剪，主打本地部署与向量检索。',
  },
  {
    id: 'b07',
    title: 'Transformers.js 本地推理',
    source: '端侧推理',
    text: 'Transformers.js 让 Hugging Face 的 Transformer 模型可以直接在浏览器里运行。它通过 ONNX Runtime Web 加载 ONNX 权重，分词和推理全部发生在用户设备上，不会把文本发送到云端。',
  },
  {
    id: 'b08',
    title: 'WebGPU 推理加速',
    source: '端侧推理',
    text: 'WebGPU 允许浏览器调用显卡执行通用计算，适合矩阵乘法密集的 Transformer 推理。启用 WebGPU 时需要显式设置推理设备，并检查适配器是否可用；成功后可以明显降低 embedding 的延迟。',
  },
  {
    id: 'b09',
    title: 'WASM 自动降级',
    source: '端侧推理',
    text: '当浏览器不支持 WebGPU、驱动异常或算子不受支持时，应用应捕获初始化错误并自动切换到 WASM 后端。降级后核心流程仍然可用，但界面必须明确提示当前使用的后端和降级原因。',
  },
  {
    id: 'b10',
    title: 'INT8 量化',
    source: '模型量化',
    text: 'INT8 量化把模型权重从 32 位浮点映射到 8 位整数，通常使用对称或非对称的逐通道缩放。ONNX Runtime 提供动态量化权重文件，体积约为 FP32 的四分之一，推理速度也常常更快。',
  },
  {
    id: 'b11',
    title: 'FP16 量化',
    source: '模型量化',
    text: 'FP16 用半精度浮点保存权重，体积约为 FP32 的一半。它在 GPU 上通常有原生支持，精度损失一般小于 INT8，但在只支持 WASM 的 CPU 环境里不一定能获得加速。',
  },
  {
    id: 'b12',
    title: '压缩比计算口径',
    source: '模型量化',
    text: '压缩比等于量化前模型文件总体积除以量化后模型文件总体积。统计时应使用同一套文件范围，例如只统计 ONNX 权重，或统一统计权重、分词器和配置文件，避免用不同口径得出夸大的结论。',
  },
  {
    id: 'b13',
    title: '模型体积测量',
    source: '模型量化',
    text: '模型体积应按实际下载到的文件字节数统计，而不是只参考参数量的理论估算。浏览器的资源计时接口可以读取解码后的响应体大小，从而核对 ONNX 权重、分词器配置等文件的真实体积。',
  },
  {
    id: 'b14',
    title: 'bge-small-zh 中文模型',
    source: 'Embedding 模型',
    text: 'BAAI/bge-small-zh-v1.5 是面向中文的轻量 embedding 模型，输出 512 维向量，适合端侧和中小规模语料。它使用 CLS 池化并做归一化，语义相近的中文句子在余弦距离下会更接近。',
  },
  {
    id: 'b15',
    title: '余弦相似度',
    source: 'Embedding 模型',
    text: '余弦相似度衡量两个向量方向的一致性，不受向量长度影响。把 embedding 归一化之后，内积与余弦相似度等价，因此向量数据库常用余弦距离做语义检索，查询向量也必须使用同一模型生成。',
  },
  {
    id: 'b16',
    title: '文本分块策略',
    source: '数据处理',
    text: '长文档需要切分成较短的片段再向量化，否则一个向量要概括过多主题。常见做法是按段落和句子边界切分，保留固定长度的重叠区，并把来源、序号等元数据一起写入，便于展示和追溯。',
  },
  {
    id: 'b17',
    title: '增量导入',
    source: '数据处理',
    text: '增量导入意味着新文档直接追加到已有集合，而不是清空重建。每批文档使用稳定且唯一的 ID 写入，重复导入时可以覆盖同一条记录，既能持续补充语料，也不会产生重复结果。',
  },
  {
    id: 'b18',
    title: '索引刷新',
    source: '数据处理',
    text: 'seekdb 写入后需要刷新向量索引，新数据才能被近似最近邻检索立即命中。应用应在导入批次结束时调用刷新接口，并把刷新耗时单独计时；增量导入后如果没有刷新，搜索可能仍然只返回旧文档。',
  },
  {
    id: 'b19',
    title: 'RRF 融合排序',
    source: '检索算法',
    text: 'RRF 即倒数排名融合，它不看两路检索的原始分数，而是按各自排名计算 1/(k+rank) 并相加。这样可以把向量语义检索和全文关键词检索融合成一个统一排序，避免不同打分体系直接相加带来的偏差。',
  },
  {
    id: 'b20',
    title: 'Recall@K',
    source: '检索评测',
    text: 'Recall@K 表示前 K 条结果中覆盖了多少比例的相关文档。计算时需要人工标注或规则生成的相关文档集合，并在量化前后使用完全相同的语料、查询和 K 值，才能比较量化对检索质量的影响。',
  },
  {
    id: 'b21',
    title: 'nDCG@K',
    source: '检索评测',
    text: 'nDCG@K 关注相关结果出现的位置，越靠前的命中贡献越大。它按折损后的增益求和，再除以理想排序的增益，适合衡量排序质量；常与 Recall@K 一起报告，避免只看召回不看顺序。',
  },
  {
    id: 'b22',
    title: '端到端延迟',
    source: '性能评测',
    text: '端到端查询延迟从用户提交查询开始，到结果渲染到界面结束，包含查询向量化、网络请求、seekdb 混合检索和前端渲染。分阶段计时可以定位瓶颈，端到端计时则反映用户真实感受。',
  },
  {
    id: 'b23',
    title: 'ONNX Runtime Web',
    source: '端侧推理',
    text: 'ONNX Runtime Web 是浏览器端的 ONNX 推理引擎，提供 WASM 和 WebGPU 两种执行后端。它支持算子融合、线程池和内存复用，Transformers.js 会把模型计算委托给它执行。',
  },
  {
    id: 'b24',
    title: 'Web Worker 与批处理',
    source: '性能优化',
    text: '在 Web Worker 中执行 embedding 可以避免推理阻塞界面渲染。把多条文本组成批次一次送入模型，能减少调用开销、提高吞吐；批次过大又会增加单次延迟和内存占用，需要实测选择。',
  },
  {
    id: 'b25',
    title: '浏览器缓存模型',
    source: '端侧推理',
    text: '首次加载模型需要下载权重，之后浏览器会把文件放入 Cache Storage。再次打开应用时可以直接从本地缓存读取，加载时间明显缩短；界面应区分首次加载和缓存命中后的加载耗时。',
  },
  {
    id: 'b26',
    title: '本地推理与隐私',
    source: '端侧推理',
    text: '端侧推理的文本、向量和检索结果都留在用户设备上，不需要上传云端。对于个人笔记、内部文档等敏感语料，这种方式可以减少数据外泄风险，也降低了服务端算力成本。',
  },
  {
    id: 'b27',
    title: '向量数据库选型',
    source: '技术选型',
    text: '选择向量数据库时要看部署方式、索引类型、混合检索能力和生态。本地应用更关注嵌入式启动、数据文件可迁移和离线可用；seekdb 同时提供向量索引、全文索引和 SQL 接口，适合这类场景。',
  },
  {
    id: 'b28',
    title: '吞吐与批大小',
    source: '性能优化',
    text: 'embedding 吞吐通常用每秒处理的文档数或字符数表示。批大小从 1 提高到 8 或 16，往往能提升吞吐，但收益会逐渐递减；应在固定设备、模型精度和后端的前提下重复测量并取中位数。',
  },
];

export const EVAL_QUERIES: EvalQuery[] = [
  { query: '在浏览器本地把中文文本编码成向量，用什么模型和运行时？', relevant: ['b14', 'b07', 'b23'] },
  { query: '浏览器不支持 WebGPU 时推理如何自动降级？', relevant: ['b09', 'b08'] },
  { query: '怎样减小 embedding 模型的下载体积？', relevant: ['b10', 'b11', 'b12', 'b13'] },
  { query: '量化前后检索质量该怎么量化评估？', relevant: ['b20', 'b21', 'b10'] },
  { query: '全文检索和向量检索的结果由谁融合排序？', relevant: ['b02', 'b19'] },
  { query: '新导入的文档搜不到，如何让索引立即生效？', relevant: ['b17', 'b18'] },
  { query: 'seekdb 可以嵌入式部署吗，数据放在哪里？', relevant: ['b05', 'b01'] },
  { query: '长文档切分时如何保留上下文？', relevant: ['b16'] },
  { query: '如何测量查询从提交到结果展示的端到端延迟？', relevant: ['b22', 'b24'] },
  { query: '怎样避免向量化计算阻塞页面交互？', relevant: ['b24', 'b07'] },
  { query: '中文语义相似度用什么模型和距离度量？', relevant: ['b15', 'b14'] },
  { query: '向量索引如何加速近似最近邻搜索？', relevant: ['b03', 'b27'] },
  { query: '模型文件第二次打开还需要重新下载吗？', relevant: ['b25', 'b26'] },
];

/** Bytes measured from the actual ONNX / tokenizer files served for Xenova/bge-small-zh-v1.5. */
export const MODEL_MANIFEST = {
  model: 'Xenova/bge-small-zh-v1.5',
  revision: 'main',
  dtype: {
    q8: {
      onnx: 24010842,
      tokenizer: 439125,
      config: 716,
      tokenizerConfig: 367,
      specialTokens: 125,
      total: 24451175,
    },
    fp32: {
      onnx: 94851877,
      tokenizer: 439125,
      config: 716,
      tokenizerConfig: 367,
      specialTokens: 125,
      total: 95292210,
    },
  },
} as const;
