# H3 提示词画廊（macOS 版）

一个本地运行的网页画廊，直接读取 Google Drive 文件夹里 MiniMax H3（以及 longcat_avatar 等）生成的图片和视频，并在旁边记录每个作品用的提示词，方便对比「什么提示词出什么效果」、后期微调。

- **不占本地空间**：图片、视频一直留在 Google Drive，网页上看的时候才实时读取（缩略图走 Drive 自带的缩略图，视频支持拖动进度条）。
- **零依赖**：只需要 macOS 自带的 Python 3.9+，不用 `pip install` 任何东西。
- **提示词存回 Drive**：提示词、负面提示词、参数、标签、备注保存在该 Drive 文件夹里的 `h3-gallery-meta.json`，换电脑也不丢。
- **自动解析文件名**：从 H3 的文件名里读出模型、模式（t2i / edit / t2va / fl2va / ai2v…）、分辨率、seed、时长、超分信息，可按类型、文件夹筛选，按同提示词 / 同 seed 分组。
- 支持收藏、隐藏、搜索、上传新作品到 Drive、自定义标题。

## 一键安装 / 升级 / 卸载

目前只支持 **macOS**。需要 Python 3.9+，macOS 自带的就行（第一次运行如果提示安装「命令行开发者工具」，点安装即可）。

**安装**：打开「终端」，粘贴下面这行回车。会装到 `~/h3-prompt-gallery`，装完自动打开画廊。

```bash
curl -fsSL https://raw.githubusercontent.com/wwintj/h3-prompt-gallery/main/install.sh | bash
```

**升级**：双击安装文件夹里的 `升级.command`，或者再运行一次上面的安装命令。只更新程序，你的授权、配置和提示词都不会动；画廊正在运行的话会自动重启。

**卸载**：双击 `卸载.command`。会先让你确认，并询问是否保留 `credentials.json`（保留的话，以后重装会自动用上，不用再去 Google Cloud 重新创建）。Google Drive 里的作品和提示词不会被删除。

> 想装到别的位置：在安装命令的 `bash` 前加上 `H3_DIR=你的路径`，比如 `... | H3_DIR=~/Apps/h3 bash`。

## 使用方法

### 1. 启动

双击 `启动画廊.command`，或者运行：

```bash
python3 ~/h3-prompt-gallery/server.py
```

然后在浏览器打开 <http://127.0.0.1:8765>。

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
| `启动画廊.command` | 双击启动 |
| `升级.command` / `upgrade.sh` | 一键升级 |
| `卸载.command` / `uninstall.sh` | 一键卸载 |
| `install.sh` | 一键安装 |
