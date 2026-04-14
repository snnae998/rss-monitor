# 🤖 RSS 监控系统

一个 全自动 RSS 监控 + Telegram 推送 + Web 面板 的工具  
👉 专门用来蹲 NodeSeek / Hostloc / Linux.do 这种信息

---

## 🚀 一句话说明

👉 基于 Python 的 RSS 监控系统，支持 **Telegram BOT 实时推送** + Web 管理面板  👉 有新帖 + 命中关键词 → 自动推送到 Telegram。

---

## ✨ 一、功能

- 🤖 Telegram BOT | 关键词匹配实时推送 

- 🌐 Web 管理面板 | 关键词管理、源管理、统计仪表盘 

- 🔍 智能过滤 | 支持排除词、正则表达式过滤 

- 📊 推送去重 | 标题相似度自动去重 

- 📄 内容预览 | 推送时显示正文摘要 

- 🛡️ 健康检查 | 源连续失败 5 次自动禁用 

- 🕐 时区修正 | 自动将 UTC+0 转换为北京时间 

- 📱 移动端适配 | 手机浏览器完美显示 

---

## ⚡ 二、3分钟部署

### ① 安装 Docker

    curl -fsSL https://get.docker.com | bash
    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose

---

### ② 创建机器人

- Telegram 搜索 @BotFather，发送 /newbot

- 输入 BOT 名称（如 RSS监控助手）

- 输入 BOT 用户名（必须以 bot 结尾，如 rss_monitor_bot）

- 保存获得的 Token

- 搜索 @userinfobot，发送 /start，保存 Chat ID

---

### ③ 下载项目

    git clone https://github.com/snnae998/rss-monitor.git
    cd rss-monitor

---

### ④ 配置变量

    cp .env.example .env
    nano .env

填写以下内容（替换为你的实际信息）：

    TG_BOT_TOKEN=你的token
    TG_CHAT_ID=你的chatid
    WEB_PASSWORD=随便写个密码
    TZ=Asia/Shanghai

---

### ⑤ 启动服务

    docker-compose up -d

---

### ⑥ 打开面板

浏览器访问：

    http://服务器IP:5000

---

## 📖 三、怎么用

### ①添加 RSS

推荐：

- https://rss.nodeseek.com/
- https://hostloc.com/forum.php?mod=rss&fid=45

---

### ②添加关键词

比如：

    VPS
    甲骨文
    Azure
    出
    收

---

## 🔧 四、常用命令

查看日志（项目目录下）：

    tail -f data/monitor.log

重启：

    docker-compose restart

停止：

    docker-compose down

---

## ❓ 五、常见问题

### ❌ BOT没反应？

👉 99% 是这个原因：

- 没加机器人到群
- 没给管理员权限 ❗

---

### ❌ 没推送？

👉 检查：

- 关键词有没有写
- RSS地址是否正常

---

## ⭐ 如果觉得好用可以点个 Star
