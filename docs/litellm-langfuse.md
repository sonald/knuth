# LiteLLM 与 Langfuse

Knuth 不封装 Langfuse 的领域模型，只把配置透传给 LiteLLM callback。默认不开启任何外部观测，避免把模型输入输出意外发出进程。

## Langfuse v3 / OTEL

LiteLLM 官方推荐 Langfuse v3 使用 OTEL callback。Knuth 已在 `knuth-llmd` 依赖中包含 LiteLLM 所需的 OTEL HTTP exporter。

```sh
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://us.cloud.langfuse.com
export KNUTH_LITELLM_CALLBACKS=langfuse_otel
```

这些变量也可以放进 repo 根目录 `.env`。`KNUTH_LITELLM_CALLBACKS` 会作为 `callbacks=["langfuse_otel"]` 传给 LiteLLM；ChatGPT/Responses 路径只有在没有显式 callback 时才保留 `no-log=True`。

## 旧版 Langfuse callback

LiteLLM 的旧 Langfuse callback 使用 success/failure callback：

```sh
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://cloud.langfuse.com
export KNUTH_LITELLM_SUCCESS_CALLBACKS=langfuse
export KNUTH_LITELLM_FAILURE_CALLBACKS=langfuse
```

旧 callback 需要 Langfuse Python SDK v2；Knuth 不默认加入该依赖。需要时再显式安装，避免和 v3/OTEL 路径混在一起。
