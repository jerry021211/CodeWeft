# 原生协作界面

- `NativeCollaboration.tsx`：对话中的执行图、画布与成员执行链路侧栏。
- `presentation.ts`：快照版本选择、依赖布局、成员状态和活动筛选。
- `useCollaborationStatus.ts`：从服务端读取权威状态，处理刷新和断线后的核对。

App 和 ChatWorkspace 负责接入此功能；通用消息、工具执行项、Task 面板仍复用原组件。
旧 Team 的组件继续用于旧记录，不能用新执行阶段直接解释旧 Candidate / Attempt。
