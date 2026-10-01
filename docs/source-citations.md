# 源码引用实验：已撤回

第三、四轮的 `source_ref` 协议已从当前产品移除。它能校验引用，但要求模型遵循另一套格式，带来额外提示、纠正请求和答案结构错误，未证明整体检索收益足以抵消这些成本。

当前恢复 B2 的正常回答流程：`search_code` 直接提供路径、函数名、行号和连续源码，模型根据用户要求回答。没有来源编号协议、专用渲染器或引用格式重试，也不把评测的 20 行要求扩展到全部回答。

原实现及失败结果仍保存在第三、四轮冻结引擎和评测目录中，供历史复核；不再作为现行接口或使用要求。

- [现行检索说明](code-search.md)
- [第三轮历史报告](code-search-round3-report-2026-09-29.md)
- [第四轮历史报告](code-search-round4-report-2026-09-29.md)
- [最后一次引用实验的冻结源码](../eval-results/code-search-round4/final-engine-v2/codeagent/source_citations.py)
