# 腾讯云轻量应用服务器部署：缠论 1分钟报告服务

本文档用于把本仓库的缠论 1分钟报告服务部署到腾讯云轻量应用服务器，并通过公网 IP 访问。

目标访问方式：

```text
http://你的公网IP/
```

部署后的行为：

- 浏览器访问公网 IP 后，先输入账号密码。
- 登录后默认生成上证指数 1分钟报告。
- 报告左上角可以选择 8 个标的并重新生成。
- 生成过程完全在服务器本地跑 Python，不调用大模型，不上传 GitHub。

部署架构：

```text
浏览器
  ↓ http://公网IP/
Nginx :80 + Basic Auth
  ↓ 反向代理
report_server.py :127.0.0.1:8765
  ↓ 调用
analyze_chan_points.py
  ↓ 输出
/opt/czsc/reports/*.html
```

参考腾讯云官方文档：

- 轻量应用服务器文档首页：https://cloud.tencent.com/document/product/1207
- 快速创建 Linux 实例：https://cloud.tencent.com/document/product/1207/44548
- 登录 Linux 实例：https://cloud.tencent.com/document/product/1207/44642
- 管理实例防火墙：https://cloud.tencent.com/document/product/1207/44577
- 价格总览：https://cloud.tencent.com/document/product/1207/73452

---

## 01. 购买前确认

本文档按以下配置编写：

```text
产品：腾讯云轻量应用服务器 Lighthouse
系统：Ubuntu Server 22.04 LTS
套餐：锐驰型
CPU：2 核
内存：4GB
系统盘：50GB SSD
公网峰值带宽：200Mbps
流量：无限流量
价格：65 元/月
访问方式：公网 IP + HTTP
访问保护：Nginx Basic Auth
```

腾讯云价格总览页面中，该规格对应：

```text
锐驰型 / 2核 / 4GB / 50GB / 200Mbps / 无限流量 / 65元/月
```

本服务对资源的压力不大。2C4G 足够个人使用，50GB 磁盘也够放代码、虚拟环境和一段时间内的 HTML 报告。

### 是否还有其它费用

第一版不需要额外购买：

- 域名：不用，直接 IP 访问。
- HTTPS 证书：不用，第一版 HTTP + Basic Auth。
- 数据库：不用。
- 对象存储 COS：不用。
- 负载均衡：不用。
- Docker 镜像仓库：不用。

可能产生的可选费用：

- 快照备份：不开就没有费用。
- 更大磁盘：后续报告很多时才需要。
- 域名和 HTTPS：后续想正式化访问时再考虑。

### 自动续费建议

如果先试用，购买 1 个月即可。确认稳定后再改成自动续费或购买更长周期。

---

## 02. 腾讯云控制台购买步骤

1. 打开腾讯云控制台。
2. 进入 **轻量应用服务器 Lighthouse**。
3. 点击 **新建实例** 或 **购买实例**。
4. 地域选择离你近的区域，例如：
   - 广州
   - 上海
   - 北京
5. 镜像选择：

```text
应用镜像 / 系统镜像：系统镜像
操作系统：Ubuntu Server 22.04 LTS
```

6. 套餐选择你截图里的配置：

```text
2核 / 4GB / 50GB / 200Mbps / 无限流量 / 65元/月
```

7. 实例名称建议：

```text
czsc-report
```

8. 登录方式：
   - 第一次使用腾讯云，建议先用密码登录。
   - 后续可以改成 SSH 密钥，更安全。

9. 购买完成后，在实例详情页记录：

```text
公网 IP：后续浏览器访问和 SSH 登录要用
用户名：Ubuntu 镜像通常使用 lighthouse 用户
```

腾讯云官方文档说明，轻量应用服务器 Linux 实例可通过控制台 OrcaTerm 免密登录，默认登录用户通常是 `lighthouse`。

---

## 03. 防火墙配置

腾讯云轻量应用服务器有自己的防火墙配置。官方文档说明，防火墙用于管理实例的入方向流量，应遵循最小授权原则。

进入实例详情页：

```text
轻量应用服务器控制台 → 实例 → 防火墙
```

保留或新增以下入站规则：

```text
TCP 22    SSH 登录
TCP 80    HTTP 访问服务
ICMP      可选，用于 ping
```

重要：不要开放 8765。

```text
TCP 8765  不开放
```

原因：

- `report_server.py` 是内部 Python 服务，只应该监听 `127.0.0.1:8765`。
- 公网访问统一走 Nginx 的 80 端口。
- 如果开放 8765，别人可能绕过 Nginx Basic Auth 直接触发生成报告。

如果腾讯云防火墙支持来源 IP 限制，建议把 22 端口限制为你自己的公网 IP。

查看自己的公网 IP 可以在本地浏览器搜索：

```text
我的 IP
```

或本地终端执行：

```bash
curl ifconfig.me
```

---

## 04. 登录服务器

### 方式 A：腾讯云控制台登录

适合第一次使用。

1. 进入轻量应用服务器实例详情页。
2. 点击 **登录**。
3. 选择腾讯云提供的 Web 终端，例如 OrcaTerm。
4. 进入终端后确认当前用户：

```bash
whoami
```

通常会输出：

```text
lighthouse
```

### 方式 B：本地 SSH 登录

本地终端执行：

```bash
ssh lighthouse@你的公网IP
```

第一次连接会提示：

```text
Are you sure you want to continue connecting?
```

输入：

```text
yes
```

然后输入服务器密码。

如果你选择的是 root 登录，则命令可能是：

```bash
ssh root@你的公网IP
```

本文档后续命令默认使用 `lighthouse` 用户，并通过 `sudo` 执行管理员操作。

---

## 05. 初始化系统

登录服务器后执行：

```bash
sudo apt update
sudo apt upgrade -y
```

安装基础软件：

```bash
sudo apt install -y git nginx apache2-utils python3 python3-venv python3-pip curl
```

说明：

- `git`：拉取 GitHub 代码。
- `nginx`：对外提供 HTTP 服务，并反向代理 Python 服务。
- `apache2-utils`：提供 `htpasswd`，用于设置访问账号密码。
- `python3-venv`：创建 Python 虚拟环境。
- `python3-pip`：安装 Python 依赖。
- `curl`：命令行测试 HTTP 服务。

确认版本：

```bash
python3 --version
nginx -v
git --version
```

---

## 06. 拉取代码

本文档使用 GitHub 拉取代码：

```text
https://github.com/josan0824/czsc.git
```

重要前提：

```text
本地最新 scripts/report_server.py 和 scripts/analyze_chan_points.py 必须已经推送到 GitHub。
```

否则服务器拉不到本地动态生成服务。

创建部署目录：

```bash
sudo mkdir -p /opt
sudo chown -R lighthouse:lighthouse /opt
```

拉取代码：

```bash
git clone https://github.com/josan0824/czsc.git /opt/czsc
cd /opt/czsc
```

检查关键文件：

```bash
ls -lh scripts/analyze_chan_points.py
ls -lh scripts/report_server.py
```

如果 `scripts/report_server.py` 不存在，说明 GitHub 仓库不是最新。需要先在本地把最新代码推到 GitHub，然后服务器执行：

```bash
cd /opt/czsc
git pull
```

---

## 07. Python 环境

进入项目目录：

```bash
cd /opt/czsc
```

创建虚拟环境：

```bash
python3 -m venv .venv
```

启用虚拟环境：

```bash
source .venv/bin/activate
```

升级 pip：

```bash
pip install --upgrade pip
```

安装依赖：

```bash
pip install requests pytdx akshare
```

依赖说明：

- `requests`：网络请求。
- `pytdx`：通达信 1分钟线数据，当前 1分钟报告的主力数据源。
- `akshare`：日线和部分备用行情源。

可选备用依赖：

```bash
pip install baostock mootdx
```

当前第一版可以不装 `baostock` 和 `mootdx`。脚本里会记录这些备用源不可用，但不影响主流程。

---

## 08. 手动验证分析脚本

创建报告目录：

```bash
mkdir -p /opt/czsc/reports
```

手动生成上证指数 1分钟报告：

```bash
cd /opt/czsc
source .venv/bin/activate

python scripts/analyze_chan_points.py \
  --stock SH000001 \
  --source web \
  --out-dir /opt/czsc/reports \
  --chart-timeframe 1m
```

成功时会输出 JSON，里面包含：

```json
"html_report": "/opt/czsc/reports/SH000001__chan_points_时间戳.html"
```

检查文件是否生成：

```bash
ls -lh /opt/czsc/reports
```

如果失败，先不要继续配置 Nginx。常见原因：

- Python 依赖没装完整。
- 服务器访问行情源失败。
- 股票代码写错。
- GitHub 拉到的代码不是最新。

---

## 09. 手动验证本地服务

先以前台方式启动服务：

```bash
cd /opt/czsc
source .venv/bin/activate

python scripts/report_server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --reports-dir /opt/czsc/reports
```

看到类似输出：

```text
Serving Chan reports at http://127.0.0.1:8765/
Reports directory: /opt/czsc/reports
```

保持这个窗口不要关闭。

再开一个 SSH 窗口，执行：

```bash
curl -I http://127.0.0.1:8765/
```

正常情况会返回：

```text
HTTP/1.0 303 See Other
```

测试生成中证1000：

```bash
curl -I "http://127.0.0.1:8765/generate?symbol=SH000852"
```

中证1000 正确代码说明：

```text
页面标题应显示：SH000852 (中证1000)
数据源代码应使用：000852
不要使用：399852
```

验证完成后，回到第一个 SSH 窗口，按：

```text
Ctrl + C
```

停止前台服务。

---

## 10. systemd 后台运行

创建 systemd 服务文件：

```bash
sudo nano /etc/systemd/system/czsc-report.service
```

粘贴以下内容：

```ini
[Unit]
Description=CZSC Chan Report Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/czsc
ExecStart=/opt/czsc/.venv/bin/python /opt/czsc/scripts/report_server.py --host 127.0.0.1 --port 8765 --reports-dir /opt/czsc/reports
Restart=always
RestartSec=5
User=lighthouse
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

保存：

```text
Ctrl + O
Enter
Ctrl + X
```

加载服务：

```bash
sudo systemctl daemon-reload
```

设置开机自启：

```bash
sudo systemctl enable czsc-report
```

启动服务：

```bash
sudo systemctl start czsc-report
```

查看状态：

```bash
sudo systemctl status czsc-report
```

正常应看到：

```text
active (running)
```

查看日志：

```bash
sudo journalctl -u czsc-report -f
```

退出日志：

```text
Ctrl + C
```

本机测试：

```bash
curl -I http://127.0.0.1:8765/
```

---

## 11. Nginx 反向代理

创建 Nginx 站点配置：

```bash
sudo nano /etc/nginx/sites-available/czsc-report
```

粘贴以下内容：

```nginx
server {
    listen 80;
    server_name _;

    client_max_body_size 20m;

    auth_basic "CZSC Report";
    auth_basic_user_file /etc/nginx/.czsc_htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        proxy_connect_timeout 10s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
    }
}
```

为什么设置 600 秒：

```text
生成 1分钟报告时需要拉行情并计算。
如果网络行情接口慢，Nginx 默认超时可能提前断开。
600 秒可以给生成任务足够时间。
```

启用站点：

```bash
sudo ln -s /etc/nginx/sites-available/czsc-report /etc/nginx/sites-enabled/czsc-report
```

删除默认站点，避免默认页面占用：

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

---

## 12. Basic Auth

创建访问账号。

这里示例用户名是：

```text
czsc
```

执行：

```bash
sudo htpasswd -c /etc/nginx/.czsc_htpasswd czsc
```

根据提示输入密码两次。

以后修改密码：

```bash
sudo htpasswd /etc/nginx/.czsc_htpasswd czsc
```

检查 Nginx 配置：

```bash
sudo nginx -t
```

正常输出：

```text
syntax is ok
test is successful
```

重载 Nginx：

```bash
sudo systemctl reload nginx
```

查看 Nginx 状态：

```bash
sudo systemctl status nginx
```

---

## 13. 公网访问验证

浏览器打开：

```text
http://你的公网IP/
```

浏览器会弹出账号密码。

输入：

```text
用户名：czsc
密码：你在 htpasswd 设置的密码
```

登录后，服务会默认生成上证指数 1分钟报告。

报告左上角可以选择 8 个标的：

```text
上证指数
平安银行
中证500
中证1000
沪深300
上证50
金山办公
立讯精密
```

重点验证中证1000：

1. 左上角选择 `中证1000`。
2. 点击生成。
3. 页面标题必须类似：

```text
SH000852 (中证1000) K线包含处理与分型测试
```

页面元信息里数据源应包含：

```text
000852
```

---

## 14. 安全检查

确认 Python 服务只监听本机：

```bash
ss -lntp | grep 8765
```

正确结果类似：

```text
127.0.0.1:8765
```

如果看到：

```text
0.0.0.0:8765
```

说明服务暴露到了公网，需要检查：

```bash
sudo systemctl cat czsc-report
```

确认里面是：

```text
--host 127.0.0.1
```

公网测试：

```text
http://你的公网IP:8765/
```

应该打不开。

腾讯云防火墙再次确认：

```text
只开放 22、80
不开放 8765
```

---

## 15. 常用运维

查看报告服务状态：

```bash
sudo systemctl status czsc-report
```

重启报告服务：

```bash
sudo systemctl restart czsc-report
```

停止报告服务：

```bash
sudo systemctl stop czsc-report
```

启动报告服务：

```bash
sudo systemctl start czsc-report
```

查看实时日志：

```bash
sudo journalctl -u czsc-report -f
```

查看最近 100 行日志：

```bash
sudo journalctl -u czsc-report -n 100 --no-pager
```

查看 Nginx 状态：

```bash
sudo systemctl status nginx
```

重载 Nginx：

```bash
sudo systemctl reload nginx
```

查看 Nginx 访问日志：

```bash
sudo tail -f /var/log/nginx/access.log
```

查看 Nginx 错误日志：

```bash
sudo tail -f /var/log/nginx/error.log
```

查看报告目录大小：

```bash
du -sh /opt/czsc/reports
```

查看磁盘空间：

```bash
df -h
```

手动清理 7 天前报告：

```bash
find /opt/czsc/reports -type f -name "*.html" -mtime +7 -delete
find /opt/czsc/reports -type f -name "*.json" -mtime +7 -delete
```

配置自动清理：

```bash
crontab -e
```

加入：

```cron
0 3 * * * find /opt/czsc/reports -type f \( -name "*.html" -o -name "*.json" \) -mtime +7 -delete
```

---

## 16. 更新代码

本地修改代码后，先推送到 GitHub。

服务器执行：

```bash
cd /opt/czsc
git pull
source .venv/bin/activate
python3 -m py_compile scripts/analyze_chan_points.py scripts/report_server.py
sudo systemctl restart czsc-report
```

如果新增 Python 依赖：

```bash
cd /opt/czsc
source .venv/bin/activate
pip install 新依赖名
sudo systemctl restart czsc-report
```

更新后验证：

```bash
sudo systemctl status czsc-report
curl -I http://127.0.0.1:8765/
```

---

## 17. 常见问题排查

### 17.1 公网 IP 打不开

检查 Nginx：

```bash
sudo systemctl status nginx
sudo nginx -t
```

检查 80 端口：

```bash
ss -lntp | grep ':80'
```

检查腾讯云防火墙：

```text
入站规则是否开放 TCP 80
```

本机测试：

```bash
curl -I http://127.0.0.1
```

### 17.2 浏览器显示 502 Bad Gateway

说明 Nginx 能访问，但后端 Python 服务异常。

检查：

```bash
sudo systemctl status czsc-report
sudo journalctl -u czsc-report -n 200 --no-pager
curl -I http://127.0.0.1:8765/
```

常见原因：

- `report_server.py` 不存在。
- Python 虚拟环境路径错误。
- systemd 里的 `User` 不存在。
- `/opt/czsc/reports` 没权限。

### 17.3 访问时一直要求账号密码

检查密码文件：

```bash
sudo ls -l /etc/nginx/.czsc_htpasswd
```

重新设置密码：

```bash
sudo htpasswd /etc/nginx/.czsc_htpasswd czsc
sudo nginx -t
sudo systemctl reload nginx
```

### 17.4 生成报告失败

查看服务日志：

```bash
sudo journalctl -u czsc-report -n 200 --no-pager
```

手动跑脚本：

```bash
cd /opt/czsc
source .venv/bin/activate

python scripts/analyze_chan_points.py \
  --stock SH000001 \
  --source web \
  --out-dir /opt/czsc/reports \
  --chart-timeframe 1m
```

如果提示缺依赖：

```bash
pip install requests pytdx akshare
```

如果提示行情接口不可用：

```text
可能是行情源临时不可用或服务器访问该接口失败。
稍后重试，或查看日志中的具体数据源错误。
```

### 17.5 中证1000 代码不对

正确：

```text
SH000852
000852
```

错误：

```text
399852
```

如果页面还显示 `399852`，说明服务器代码不是最新。

执行：

```bash
cd /opt/czsc
git pull
sudo systemctl restart czsc-report
```

### 17.6 服务器重启后服务没恢复

检查 systemd 是否启用：

```bash
sudo systemctl is-enabled czsc-report
```

如果不是 `enabled`：

```bash
sudo systemctl enable czsc-report
sudo systemctl start czsc-report
```

检查 Nginx：

```bash
sudo systemctl is-enabled nginx
sudo systemctl status nginx
```

---

## 18. 验收清单

部署完成后逐项确认：

```text
[ ] 腾讯云防火墙只开放 22、80，没有开放 8765
[ ] /opt/czsc 存在代码
[ ] /opt/czsc/scripts/report_server.py 存在
[ ] /opt/czsc/.venv 存在
[ ] 手动执行 analyze_chan_points.py 可以生成报告
[ ] systemctl status czsc-report 是 active (running)
[ ] systemctl status nginx 是 active (running)
[ ] http://公网IP/ 会弹账号密码
[ ] 登录后默认生成上证指数
[ ] 左上角可以选择 8 个标的
[ ] 中证1000 显示 SH000852 (中证1000)
[ ] http://公网IP:8765/ 不可访问
[ ] reboot 后服务自动恢复
```

重启验证：

```bash
sudo reboot
```

等待 1-2 分钟后重新 SSH：

```bash
ssh lighthouse@你的公网IP
```

检查：

```bash
sudo systemctl status czsc-report
sudo systemctl status nginx
```

浏览器再次访问：

```text
http://你的公网IP/
```

---

## 19. 后续可选增强

第一版先不做，后续可以逐步加：

- 绑定域名。
- 配置 HTTPS。
- 更漂亮的生成中页面。
- 报告列表页。
- 每个标的只保留最近 N 份报告。
- 后台任务队列和进度轮询。
- 用正式登录页面替代 Basic Auth。
- Docker 化部署。
