# 部署：后台常驻，以及和 OneBot 协议端并排运行

这份文档讲两件事：怎么让小屋一直在后台运行，以及怎么让它连上一个 OneBot v11 协议端（本项目优先对齐 SnowLuma）。
第一次试用不需要读这份文档，`ai-house up` 加网页聊天就够了（见 [README](../README.md)）。接 QQ 的行为、限速和风险见
[CHAT-AND-QQ.md](CHAT-AND-QQ.md)，请先读那一份。

> **现状**：下面的模板做过语法校验（`systemd-analyze verify`、`docker compose config`），`doctor --onebot-ping` 和
> 停止信号的处理有自动化测试。但整套流程还没有在真实的 QQ 账号和真实的 SnowLuma 上跑过。第一次接请用测试群和小号。

## 1. 后台常驻

两种方式选一种，不要同时对同一个数据目录用两种（第二个实例会拒绝启动）。

### 1.1 Docker Compose

```sh
cp .env.example .env
```

在 `.env` 里填 `AI_HOUSE_OWNER_TOKEN`（至少 16 位，容器里必须设）和模型配置，然后先用默认方式确认能启动：

```sh
docker compose up -d --build
```

```sh
docker compose exec ai-house ai-house doctor
```

确认没问题后，叠加常驻配置（宿主机或 Docker 重启后自动拉起）：

```sh
docker compose -f compose.yaml -f deploy/compose.always-on.yaml up -d
```

- 数据在命名卷 `house-data` 里，`docker compose down` 不会删它；`down -v` 会。
- 默认只发布到宿主机的 `127.0.0.1:8780`。要从别的设备访问，请在前面放一个带 HTTPS 的反向代理，不要直接把端口改成对外发布。
- 停止：`docker compose stop`。小屋收到停止信号后会保存状态再退出，最多等 30 秒。

### 1.2 systemd（不用 Docker）

模板在 [`deploy/systemd/ai-house.service`](../deploy/systemd/ai-house.service)。它假设程序装在
`/opt/ai-house-kit/.venv`，所有会变的东西（`.env`、数据库、媒体、自定义世界包）都在 `/var/lib/ai-house`，以专用用户
`aihouse` 运行。路径不一样时改模板里的三处。

```sh
sudo useradd --system --home-dir /var/lib/ai-house --shell /usr/sbin/nologin aihouse
```

```sh
sudo git clone https://github.com/Gengetau/ai-house-kit /opt/ai-house-kit
```

```sh
sudo python3 -m venv /opt/ai-house-kit/.venv
```

```sh
sudo /opt/ai-house-kit/.venv/bin/pip install "/opt/ai-house-kit[onebot]"
```

不接 QQ 的话可以去掉 `[onebot]`。接着准备数据目录和 `.env`：

```sh
sudo install -d -m 0700 -o aihouse -g aihouse /var/lib/ai-house
```

```sh
sudo install -m 0600 -o aihouse -g aihouse /opt/ai-house-kit/.env.example /var/lib/ai-house/.env
```

编辑 `/var/lib/ai-house/.env` 填好模型配置（只监听本机时可以不设令牌），然后检查、安装并启动：

```sh
sudo -u aihouse /opt/ai-house-kit/.venv/bin/ai-house doctor --env-file /var/lib/ai-house/.env --data-dir /var/lib/ai-house/runtime
```

```sh
sudo install -m 0644 /opt/ai-house-kit/deploy/systemd/ai-house.service /etc/systemd/system/ai-house.service
```

```sh
sudo systemctl daemon-reload
```

```sh
sudo systemctl enable --now ai-house
```

看状态和日志：

```sh
systemctl status ai-house
```

```sh
journalctl -u ai-house -f
```

- 进程异常退出会在 5 秒后自动重启；5 分钟内连续失败 5 次就停下来。
- 配置有问题（令牌、端口被占、QQ 接入配置写错、同一数据目录已有实例）时退出码是 2，不会自动重启，改好后 `sudo systemctl restart ai-house`。
- `systemctl stop` 会让小屋保存状态、关闭通道、释放运行锁后退出。
- 备份：`sudo -u aihouse /opt/ai-house-kit/.venv/bin/ai-house backup --data-dir /var/lib/ai-house/runtime`；恢复前先 `systemctl stop ai-house`（见 [CHAT-AND-QQ.md](CHAT-AND-QQ.md) 第 5 节）。

## 2. 和 OneBot 协议端并排运行

小屋这边只做一件事：主动去连协议端的**正向 WebSocket 服务**（协议端监听，小屋连过去）。所以协议端那边需要：

1. 登录好给室友用的 QQ 小号；
2. 开一个**正向 WebSocket 服务端**（不是 HTTP，也不是反向 WebSocket），并且启用；
3. 给这个服务设一个访问令牌；
4. 消息上报格式选**数组（array）**。

### 2.1 SnowLuma 侧要点

SnowLuma 是独立的第三方项目，本项目和它没有从属关系，也**不打包、不分发**它（它的许可是「源码可见、仅限非商业使用」，
二进制另有自己的用户协议）。安装、登录和升级请以它自己的文档为准。下面几条是 2026-11 从它的仓库说明和文档里查到的，写在这里只是为了让你知道该找什么：

- 管理界面（WebUI）默认在 `5099` 端口；首次登录的初始密码打印在它的启动日志里。
- 在 WebUI 里新建一个 `wsServers` 类型的连接（这就是正向 WebSocket 服务端，默认端口 `3001`），**启用**它，消息格式选 `array`，并记下它的 `accessToken`。`wsClients` 是反方向的，不要选。
- WebUI 的登录密码和 OneBot 的 `accessToken` 是两回事；把前者填进 `ONEBOT_ACCESS_TOKEN` 会得到 401。
- Docker 版通过浏览器里的远程桌面（noVNC，默认 `6081`）扫码登录，并且需要它自己 compose 文件里写的那些额外权限。请直接用它仓库里的 compose 文件，不要从别处抄。

### 2.2 三种摆法

| 小屋 | 协议端 | `ONEBOT_WS_URL` |
|---|---|---|
| 直接运行 / systemd | 同一台机器上直接运行 | `ws://127.0.0.1:3001` |
| Docker | 宿主机上直接运行 | `ws://host.docker.internal:3001` |
| Docker | Docker | `ws://<协议端的服务名>:3001`（见下） |

**小屋在 Docker、协议端在宿主机**：`compose.yaml` 已经配好了 `host.docker.internal`。协议端的 WebSocket 服务端要监听在
Docker 网桥够得着的地址上（只监听 `127.0.0.1` 的话容器连不进来），同时用防火墙保证这个端口不对外网开放，并且一定要设令牌。

**两边都在 Docker**：用 [`deploy/onebot/compose.onebot.yaml`](../deploy/onebot/compose.onebot.yaml) 让两个容器进同一个网络，
协议端的 WebSocket 端口就不必发布到宿主机。文件开头有逐步说明，简单说是三步：

```sh
docker network create onebot
```

在协议端自己的 compose 里让它的服务加入外部网络 `onebot`，然后：

```sh
docker compose -f compose.yaml -f deploy/onebot/compose.onebot.yaml up -d
```

需要常驻就把三个文件叠在一起：`-f compose.yaml -f deploy/compose.always-on.yaml -f deploy/onebot/compose.onebot.yaml`。

### 2.3 小屋这边的配置

在 `.env` 里（注释必须单独占一行）：

```ini
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=
# 你本人的 QQ 号，不是室友登录的那个号
ONEBOT_OWNER_QQ=123456789
# 允许回话的群；不写就不进任何群
ONEBOT_GROUPS=
```

全部选项和它们的含义见 [CHAT-AND-QQ.md](CHAT-AND-QQ.md)。

### 2.4 检查连通

```sh
ai-house doctor --onebot-ping
```

Docker 里：

```sh
docker compose exec ai-house ai-house doctor --onebot-ping
```

它会按小屋平时的方式连一次协议端，问一下「是哪个实现、登录了没有」，然后断开。**不会发任何消息**，输出里不会出现令牌和 QQ 号。常见结果：

| 提示 | 原因和处理 |
|---|---|
| 已连上 …，协议端已登录 QQ | 正常 |
| 连不上 … | 协议端没启动、端口不对或被防火墙挡住；小屋在 Docker 里时地址不能写 `127.0.0.1` |
| 拒绝了连接（HTTP 401/403） | `ONEBOT_ACCESS_TOKEN` 和协议端里设的不一致，或者一边设了一边没设 |
| 不是 OneBot 的正向 WebSocket 端口 | 连到了 HTTP 端口或 WebUI 端口；确认开的是正向 WebSocket 服务端并且已启用 |
| 已连上，但协议端还没有登录 QQ | 先在协议端完成登录 |
| 没有收到应答 | 这个端口不是 OneBot v11 接口 |
| `ONEBOT_OWNER_QQ` 里填的是室友自己登录的这个号 | 那里要填你本人的号 |

连通之后启动小屋，用你本人的号给室友的小号发一条私聊。没有回复时先看小屋日志里 `[onebot_v11]` 开头的行，再对照
[CHAT-AND-QQ.md](CHAT-AND-QQ.md) 的「它怎么表现」一表（没配模型、室友在睡觉、超过限速时都会保持安静）。
