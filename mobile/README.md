# CodeWeft 手机端（iOS）

当前 Expo 工程以 `mobile/` 为启动目录，只启用 iOS。`src/ios/` 放置 iPhone 聊天界面、模型设置和钥匙串读写。`src/shared/` 放置模型请求、流式回复解析和 SQLite 会话逻辑。以后开发 Android APK 时，Android 入口可以直接复用共享层，并单独配置包名、签名和设备端存储。

iPhone 通过 HTTPS 直接请求个人模型服务。首版支持带 Bearer API Key 的 Chat Completions 文字流。服务地址可以填写包含版本路径的基础地址，如 `https://你的服务域名/v1`。模型 ID 使用服务商实际提供的名称。

## 在 iPhone 上体验

在 Windows 项目目录执行以下命令，电脑和手机连接同一 Wi-Fi。

```powershell
cd D:\new_codeAgent\mobile
npm ci
npm start
```

在 iPhone 安装支持 Expo SDK 58 的 Expo Go，用相机扫终端中的二维码。点击「配置模型」，填写服务地址、模型 ID 和个人 API Key，保存后输入第一条消息。生成时点击停止键会保存已收到的文字。「历史」可打开旧会话，「＋」可开始新会话。

Expo Go 开发预览需要电脑提供开发包。要在关机后独立冷启动，请安装包含 JS 包的独立 iOS 构建。项目提供内部测试构建配置。在 mobile 目录使用自己的 Expo 与 Apple 开发者账号运行 `npx eas-cli device:create` 注册 iPhone，再运行 `npx eas-cli build --platform ios --profile preview`。按提示关联自己的项目、选择可用的 Bundle ID 并签名，通过构建完成页的链接安装。此流程会把构建源码交给 Expo，Apple 设备注册与签名要求以账号为准。

## 连接电脑上的项目

在同一台电脑运行工作台和本目录的 Expo 开发服务器，手机连接可信的同一 Wi-Fi。首次升级要重新启动两个服务以加载新版接口和 Metro 转发配置。工作台默认端口为 8765；使用其他端口时，在启动 Expo 的终端设置 `CODEWEFT_WEB_PORT`。

在工作台「设置 → 连接手机」生成一次性配对码，在 iPhone 聊天顶部点「切换电脑」后粘贴。选择项目可打开现有会话或新建对话。首版支持文字历史与只读提问，回复完成后自动显示；编辑、命令审批和管理团队任务仍在电脑上操作。

电脑仅在内存中保存当前手机的配对凭证。后端重启或电脑端撤销连接后需要重新配对。Expo 开发服务器只转发加密数据包，手机本地聊天的 API Key 和电脑项目的配对凭证分别保存。

## 数据与恢复

手机直连模型的聊天记录存在本机 SQLite，最近打开的会话在下次启动时恢复。API 配置存入设备钥匙串。切换服务地址需要重新输入密钥，新地址使用新会话。回复中断时保留已写入的部分并显示状态。App 重新启动后由用户决定是否再次提问。手机仍需能联网访问所填 API。

在 mobile 目录运行 `npm test`、`npm run typecheck` 和 `npm run export:ios` 可验证协议、存储与 iOS 打包。测试使用 Node 24。
