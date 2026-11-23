# H3 提示词画廊

一个本地运行的网页画廊，直接读取 Google Drive 文件夹里 MiniMax H3（以及 longcat_avatar 等）生成的图片和视频，并在旁边记录每个作品用的提示词，方便对比「什么提示词出什么效果」、后期微调。

- **不占本地空间**：图片、视频一直留在 Google Drive，网页上看的时候才实时读取（缩略图走 Drive 自带的缩略图，视频支持拖动进度条）。
- **零依赖**：只需要 macOS / Linux / Windows 自带的 Python 3.9+，不用 `pip install` 任何东西。
- **提示词存回 Drive**：提示词、负面提示词、参数、标签、备注保存在该 Drive 文件夹里的 `h3-gallery-meta.json`，换电脑也不丢。
- **自动解析文件名**：从 H3 的文件名里读出模型、模式（t2i / edit / t2va / fl2va / ai2v…）、分辨率、seed、时长、超分信息，可按类型、文件夹筛选，按同提示词 / 同 seed 分组。
- 支持收藏、隐藏、搜索、上传新作品到 Drive、自定义标题。

## 使用方法

### 1. 启动

```bash
python3 server.py
```

然后在浏览器打开 <http://127.0.0.1:8765>。macOS 上也可以直接双击 `启动画廊.command`。

### 2. 创建自己的 Google OAuth 客户端（只需一次）

每个人都要用**自己的** Google Cloud 项目，网页的设置向导里有一样的步骤和链接：

1. 在 [Google Cloud 控制台](https://console.cloud.google.com/projectcreate) 新建一个项目。
2. 启用 [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)。
3. 在 [Google Auth Platform](https://console.cloud.google.com/auth/overview) 配置应用：受众选「外部」，并在「测试用户」里加上你自己的 Gmail。
4. 「客户端」→「创建客户端」→ 类型选 **桌面应用** → 下载 JSON。
5. 把下载的文件改名为 `credentials.json`，放在 `server.py` 同一个文件夹里。

### 3. 登录并选择文件夹

回到网页，点「登录 Google」授权，然后粘贴存放作品的 Drive 文件夹链接（子文件夹会一起读取）。

> 应用处于「测试」状态时，Google 的登录有效期是 7 天，过期后网页会提示重新登录。

## 隐私与安全

- 服务器只监听 `127.0.0.1`，并校验 Host 头和请求头，防止其他网页偷偷调用。
- 需要完整的 Drive 权限是为了能把 `h3-gallery-meta.json` 和上传的作品写进你**已有**的文件夹；程序**不会删除** Drive 里的任何文件，「隐藏」只是做个标记。
- `credentials.json`、`token.json`、`config.json`、`meta-backup.json` 都只保存在本机，已在 `.gitignore` 中排除，**不要把它们上传或发给别人**。

## 文件说明

| 文件 | 作用 |
|---|---|
| `server.py` | 本地服务器：Google 登录、读取 Drive、代理缩略图和视频、保存提示词 |
| `index.html` | 画廊网页 |
| `favicon.svg` | 浏览器标签图标 |
| `启动画廊.command` | macOS 双击启动脚本 |
