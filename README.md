# Address2IP Demo

这个项目现在包含两个核心脚本和一个浏览器面板：

- `receiver.py`: 启动本地 HTTP 接收服务，同时提供请求可视化前端。
- `send_with_source_ip.py`: 发 HTTP 请求时显式绑定本地源 IP。
- `web/`: 浏览器前端资源，用来实时查看接收到的请求。

## 适用范围

这个方案只能绑定“当前机器已经拥有、并且系统可路由”的本地地址。

它不能伪造任意公网来源 IP。对于普通 HTTP/TCP 请求，服务端看到的来源 IP 仍然必须是操作系统实际可用的本地地址。

## 启动可视化接收服务

```bash
python3 receiver.py --host 0.0.0.0 --port 18080
```

启动后会提供两个用途：

- 浏览器面板: `http://127.0.0.1:18080/`
- 接收测试请求: `http://127.0.0.1:18080/demo`

说明：

- `/` 是前端页面。
- `/api/requests`、`/api/summary`、`/api/clear` 是前端使用的接口。
- 除这些 UI/API 路由外，其余路径都会被当作“被测试请求”记录下来。

## 浏览器里能看到什么

前端页面会实时刷新并显示：

- 总请求数
- 来源 IP 数量
- 最近一次请求时间
- 请求方法分布
- 最近请求列表
- 某条请求的完整 Header、Body 和原始 JSON

页面支持：

- 按来源 IP 过滤
- 按方法过滤
- 按路径/Header/Body 关键词搜索
- 一键清空已记录请求

## 发送测试请求

最稳妥的做法是先查看本机已经存在的地址：

```bash
ifconfig
```

这次在本机实测时，已有地址是：

- `127.0.0.1`
- `172.27.23.97`

然后把目标 URL 指到本地接收服务，比如：

```bash
python3 send_with_source_ip.py \
  http://127.0.0.1:18080/demo \
  --source-ip 127.0.0.1 \
  --header "X-Demo: loopback-self"
```

```bash
python3 send_with_source_ip.py \
  http://172.27.23.97:18080/demo \
  --source-ip 172.27.23.97 \
  --header "X-Demo: lan-self"
```

发送完成后，刷新浏览器页面或者等它自动刷新，就能直接看到 `client_ip` 是否如预期变化。

## 对真实局域网地址使用

如果你要让服务端看到真实局域网 IP，例如 `192.168.1.x`：

- 这些地址必须已经配置在你的网卡或网络命名空间上。
- 路由必须允许这些地址作为本机源地址发出。
- 目标 URL 对这些来源地址必须可达。

只要这些前提成立，调用方式和上面的示例一致，只需要把 `--source-ip` 换成你的实际局域网地址即可。

## 示例

```bash
python3 send_with_source_ip.py \
  http://192.168.1.20:8080/ping \
  --method POST \
  --source-ip 192.168.1.101 \
  --source-ip 192.168.1.102 \
  --header "Content-Type: application/json" \
  --data '{"hello":"world"}'
```

## 已知注意点

- Linux 上很多环境里 `127.0.0.0/8` 可以直接使用。
- macOS 默认通常只有 `127.0.0.1`，没有额外配置时绑定 `127.0.0.2` 之类地址会报 `Can't assign requested address`。
- `receiver.py` 的日志目前保存在内存里，重启服务后会清空。

## 监控路由器对外流量

如果你要监控的是“整台路由器对外的 WAN 流量”，和这个项目里现有的 `receiver.py` / `send_with_source_ip.py` 不是一回事。

关键区别是：

- 现有脚本只能看某一台主机发出的 HTTP 请求。
- 路由器对外流量监控需要能拿到路由器 WAN 口的累计计数器，例如 `rx_bytes`、`tx_bytes`。
- 如果脚本运行在一台普通内网电脑上，它默认看不到“其他所有终端经过路由器出去的总流量”。

因此，真正可落地的方案通常只有三种：

- 路由器本身提供可轮询的管理接口。
- 你把一台 Linux/OpenWrt 设备放到网关位置。
- 交换机支持端口镜像，你把镜像口接到抓包机。

### 针对华为 AX3 的现实限制

根据华为官网公开资料，目前能确认的是：

- AX3 的 Web 管理地址是 `192.168.3.1`。
- 智慧生活 App 可以查看“运行周报”，里面包含“当周使用总流量”和接入设备流量排行。
- 运行周报是周级统计，不是秒级实时监控。
- 重启路由器会清零当天的流量统计。
- IPv6 流量当前不在这份统计里。

我没有查到华为对 AX3 公开文档化的 SNMP 或 HTTP API 说明。这不代表设备一定没有内部接口，只代表不能把“官方 API 名字和字段”写死在脚本里。

如果你抓到的是下面这种响应：

- `DownBandwidth`
- `UpBandwidth`
- `DownBandwidthHistory`
- `UpBandwidthHistory`

那么从字段名和数值分布来看，我推断：

- `DownBandwidth` / `UpBandwidth` 是当前下行/上行带宽值。
- `DownBandwidthHistory` / `UpBandwidthHistory` 是最近一段时间的历史带宽序列。
- 这些值大概率是 `Kbps`，不是累计字节数。

这部分是基于你抓到的 JSON 字段做的推断，不是华为公开文档里的明确单位定义。最稳妥的验证方式是：你在一台终端上发起大文件下载，同时观察 `DownBandwidth` 是否明显跳高。

### 已提供的脚本

仓库里新增了 `router_traffic_monitor.py`，用途是：

- 轮询一个返回 JSON 的本地管理接口。
- 从 JSON 中取出累计的入站/出站字节计数器。
- 或者直接读取当前上下行带宽值。
- 自动换算成当前上下行速率。
- 可选写入 CSV，方便后续画图。

脚本只依赖 Python 标准库。

### 你需要先做的事

先自己在浏览器里确认 AX3 管理页到底有没有可脚本化的流量接口：

1. 电脑连上 AX3 的 LAN 或 Wi-Fi。
2. 打开 `http://192.168.3.1` 登录管理页面。
3. 打开浏览器开发者工具 `Network` 面板。
4. 在路由器页面里点击可能展示流量、设备报告、统计信息的位置。
5. 过滤 `fetch` / `xhr` 请求，找出返回 JSON 的接口。
6. 确认响应里是否存在累计字节字段，例如 `rx_bytes`、`tx_bytes`、`downloadBytes`、`uploadBytes` 这一类。

如果看到的是前端页面文本而不是 JSON，或者压根没有相关请求，那就说明这条路大概率走不通。

### 用法示例

下面是一个示意命令，字段名需要替换成你在 AX3 页面里实际抓到的内容：

```bash
python3 router_traffic_monitor.py \
  "http://192.168.3.1/api/traffic-stat" \
  --cookie "SessionID=replace_me" \
  --header "X-Requested-With: XMLHttpRequest" \
  --in-path "data.wan.rx_bytes" \
  --out-path "data.wan.tx_bytes" \
  --interval 3 \
  --print-json-once \
  --csv wan_traffic.csv
```

### 你的 AX3 可直接试的命令

基于你已经抓到的这个接口：

- URL: `http://192.168.3.1/api/ntwk/wan?type=active`
- Header: `_responseformat: JSON`
- Header: `X-Requested-With: XMLHttpRequest`
- Header: `Referer: http://192.168.3.1/html/index.html`
- Cookie: `SessionID_R3=...`
- 当前带宽字段: `DownBandwidth` / `UpBandwidth`

可以先直接跑：

```bash
python3 router_traffic_monitor.py \
  "http://192.168.3.1/api/ntwk/wan?type=active" \
  --header "_responseformat: JSON" \
  --header "X-Requested-With: XMLHttpRequest" \
  --header "Referer: http://192.168.3.1/html/index.html" \
  --header "Accept: application/json, text/javascript, */*; q=0.01" \
  --cookie "SessionID_R3=replace_me" \
  --in-path "DownBandwidth" \
  --out-path "UpBandwidth" \
  --value-mode rate \
  --rate-unit Kbps \
  --interval 2 \
  --transport curl \
  --csv wan_traffic.csv
```

如果我的 `Kbps` 推断是对的，输出会类似：

```text
[2026-04-23T21:10:03+08:00] rx_rate=0.14 Mbps (17.25 KB/s) tx_rate=0.06 Mbps (8.00 KB/s) raw_rx=138 Kbps raw_tx=64 Kbps
```

如果你下载文件时数值明显不对，比如应该有几十兆但这里只显示很小，那就把 `--rate-unit Kbps` 改成别的单位再试。

如果接口是 HTTPS 且证书是路由器自签名证书，可以加：

```bash
--insecure
```

如果接口需要 POST，可以加：

```bash
--method POST --body '{"query":"wan"}'
```

脚本输出示例：

```text
[2026-04-23T21:00:03+08:00] baseline rx_total=3.21 GB tx_total=412.53 MB
[2026-04-23T21:00:06+08:00] rx_rate=18.42 Mbps (2.20 MB/s) tx_rate=1.83 Mbps (234.00 KB/s) rx_total=3.22 GB tx_total=413.20 MB
```

### JSON 路径怎么写

如果接口返回：

```json
{
  "data": {
    "wan": {
      "rx_bytes": 3456789012,
      "tx_bytes": 456789012
    }
  }
}
```

那么参数就是：

```bash
--in-path data.wan.rx_bytes
--out-path data.wan.tx_bytes
```

如果是数组，也支持：

```bash
--in-path data.interfaces[0].rx_bytes
--out-path data.interfaces[0].tx_bytes
```

### 如果 AX3 没有可用接口

那就不要在普通内网电脑上硬写“全网总流量监控”脚本，因为它天然看不到所有设备的总出口流量。

更靠谱的替代方案是：

- 把网关换成 OpenWrt / Linux 软路由，然后直接读 WAN 网卡计数器。
- 使用支持 SNMP 的企业级路由器或交换机。
- 使用带镜像口的交换机，把 AX3 的上联口镜像到抓包机上。

## 记录哪台内网 IP 访问了某个目标 URL

如果你的真实目标是：

- 哪个连接 AX3 的内网 IP 发起了请求
- 请求是不是去了某个指定站点或 URL
- 最好还能留下时间、方法、状态码这类记录

那么要先把边界讲清楚：

- 只是在一台普通局域网电脑上运行脚本，默认看不到“所有其他设备经过路由器出去的流量”。
- AX3 原厂固件也没有公开、稳定的“按内网 IP 列出每条外连 URL”接口可直接轮询。
- 对 HTTPS 请求，不做中间人解密时，通常只能稳定看到目标域名，无法看到加密后的完整 URL 路径。

所以真正可落地的方案只有两类：

- 让监控机进入流量路径，例如做网关、透明代理、镜像口抓包。
- 让客户端显式通过代理访问外网，然后在代理上记录来源 IP 和目标地址。

### 已提供的代理脚本

仓库里新增了 `proxy_request_monitor.py`，这是一个显式 HTTP/HTTPS 代理，作用是：

- 客户端把它配置成代理后，所有请求会先到这台监控机。
- 脚本会把请求继续转发到真实目标站点。
- 同时记录是哪个内网 IP 发起的请求。
- 对 HTTP 请求，可以记录完整 URL。
- 对 HTTPS 请求，可以记录 CONNECT 的目标主机和端口，但看不到加密后的路径。
- 默认还会额外启动一个 Web 面板，实时查看命中记录。

### 为什么这个方案更接近你的目标

你要的不是总带宽，而是这种记录：

```json
{
  "timestamp": "2026-04-23T21:30:00+08:00",
  "client_ip": "192.168.3.23",
  "method": "GET",
  "host": "example.com",
  "path": "/api/order/123",
  "url": "http://example.com/api/order/123"
}
```

这一类信息，最自然的采集点就是代理，而不是 WAN 流量统计接口。

### 直接运行

```bash
python3 proxy_request_monitor.py \
  --listen-host 0.0.0.0 \
  --listen-port 18090 \
  --match-host example.com \
  --log-file proxy_hits.jsonl
```

启动后，终端会输出：

```text
Proxy request monitor listening on 0.0.0.0:18090
dashboard: http://127.0.0.1:18091/
```

如果你希望从其他设备打开面板，可以加：

```bash
--dashboard-host 0.0.0.0
```

### 客户端怎么接入

让需要监控的设备把代理指向这台机器的局域网地址，例如：

- 代理主机：`192.168.3.100`
- 代理端口：`18090`

如果只想先验证浏览器，可以先在浏览器或系统代理里手动配置：

- HTTP 代理：`192.168.3.100:18090`
- HTTPS 代理：`192.168.3.100:18090`

然后访问目标站点，脚本就会打印匹配到的记录。

### Web 面板

默认情况下，`proxy_request_monitor.py` 会在单独的 `18091` 端口启动 dashboard。

打开：

```text
http://127.0.0.1:18091/
```

面板支持：

- 实时刷新命中日志
- 按设备 IP 过滤
- 按目标主机过滤
- 按模式过滤（HTTP Proxy / HTTPS CONNECT）
- 按全文检索 URL、主机、备注
- 点击左侧记录查看完整 JSON 明细
- 一键清空当前内存中的命中日志

之所以单独用一个 dashboard 端口，而不是和代理端口复用，是为了避免浏览器把面板请求本身也当成代理流量，造成访问冲突。

### 过滤指定目标

只记录某个域名：

```bash
--match-host example.com
```

只记录某个 URL 片段：

```bash
--match-url-contains /api/order
```

只记录某个 HTTP 路径片段：

```bash
--match-path-contains /login
```

这些过滤条件是同时生效的。

例如：

```bash
python3 proxy_request_monitor.py \
  --listen-port 18090 \
  --match-host example.com \
  --match-path-contains /api/order
```

### 输出示例

普通 HTTP 请求会记录完整 URL：

```json
{"timestamp":"2026-04-23T21:30:00+08:00","mode":"http-proxy","client_ip":"192.168.3.23","client_port":51744,"method":"GET","scheme":"http","host":"example.com","port":80,"path":"/api/order/123","url":"http://example.com/api/order/123","request_body_bytes":0,"response_status":200,"response_body_bytes":4821,"duration_ms":143}
```

HTTPS 请求只会记录目标主机：

```json
{"timestamp":"2026-04-23T21:31:10+08:00","mode":"https-connect","client_ip":"192.168.3.23","client_port":51758,"method":"CONNECT","host":"example.com","port":443,"note":"HTTPS CONNECT can log target host, but not the encrypted URL path.","client_to_server_bytes":1842,"server_to_client_bytes":92341,"duration_ms":2104}
```

### 这个方案的限制

- 只有“显式通过这个代理访问”的流量，才会被记录。
- 没有配置代理的设备，不会自动出现在日志里。
- 对 HTTPS，不安装自定义根证书并做 MITM 解密时，无法拿到完整路径。
- 某些 App 会绕过系统代理、直接连网，这类请求也抓不到。

### 如果你想监控全局而不是手动配代理

那就不是这个仓库里一段普通 Python 脚本能单独解决的了。你需要：

- 把 OpenWrt / Linux 机器放到网关位置，做透明代理或抓包。
- 或者交换机支持端口镜像，把 AX3 的出口镜像到抓包机。
- 或者路由器本身支持更细的连接日志导出。

## DNS 监控：记录哪个设备查询过 B 站域名

如果不想给每台设备手动配置 HTTP 代理，但目标可以降级为：

- 哪个内网 IP 查询过 `bilibili.com` 相关域名
- 什么时候查询的
- 查询了 A / AAAA / HTTPS 等哪类 DNS 记录

可以用 `dns_monitor.py`。

它的工作方式是：

```text
手机/电脑 -> 这台监控机 DNS -> 上游 DNS
```

脚本会记录 DNS 查询，然后把查询转发给真实上游 DNS，不影响正常解析。

### 适合什么

- 判断“哪个设备很可能访问了 B 站相关服务”
- 观察 B 站 App 或网页会查哪些域名
- 不想逐台设置 HTTP 代理时，做低侵入监控

### 不适合什么

- 不能证明每次 DNS 查询后一定真的发起了连接
- 看不到 HTTPS 完整 URL
- 设备启用 DoH / 私有 DNS / 缓存 / 硬编码 DNS 时可能绕过
- 如果只在单台手机手动改 DNS，其他设备不会被监控

### 启动 DNS 监控

macOS / Linux 上监听 `53` 端口通常需要管理员权限：

```bash
sudo python3 dns_monitor.py \
  --listen-host 0.0.0.0 \
  --listen-port 53 \
  --dashboard-host 0.0.0.0 \
  --dashboard-port 18092 \
  --upstream 10.8.8.8 \
  --upstream 10.8.4.4 \
  --match-domain bilibili.com \
  --match-domain hdslb.com \
  --match-domain bilivideo.com \
  --log-file dns_hits.jsonl
```

如果只是本机测试，可以用高端口，不需要 `sudo`：

```bash
python3 dns_monitor.py \
  --listen-host 127.0.0.1 \
  --listen-port 1053 \
  --dashboard-port 18092 \
  --match-domain bilibili.com
```

打开面板：

```text
http://127.0.0.1:18092/
```

如果 dashboard 绑定到 `0.0.0.0`，也可以从局域网打开：

```text
http://你的电脑局域网IP:18092/
```

### 让 AX3 下发这个 DNS

要监控局域网设备，关键不是脚本启动，而是让设备真的把 DNS 请求发到这台电脑。

推荐做法：

1. 给这台电脑固定一个局域网 IP，例如 `192.168.3.208`。
2. 登录 AX3 管理页面 `http://192.168.3.1`。
3. 找到 DHCP / LAN / DNS 相关设置。
4. 把 DHCP 下发的 DNS 服务器改为 `192.168.3.208`。
5. 保存后，让手机断开重连 Wi-Fi，或者关闭再打开 Wi-Fi。
6. 打开 DNS 面板，看是否出现查询记录。

如果 AX3 不允许修改 LAN DHCP DNS，那就只能在单个设备上手动把 DNS 改成 `192.168.3.208`，或者继续使用显式代理方案。

### B 站建议匹配域名

B 站不只使用 `bilibili.com`。建议至少监控：

- `bilibili.com`
- `hdslb.com`
- `bilivideo.com`

必要时可以先不加 `--match-domain`，观察一段时间全部 DNS，再从里面筛选 B 站相关域名。

### 输出示例

```json
{"timestamp":"2026-04-23T22:10:00+08:00","client_ip":"192.168.3.23","client_port":51432,"protocol":"udp","domain":"api.bilibili.com","qtype":"A","qtype_code":1,"qclass":1,"matched":true,"upstream":"10.8.8.8:53","rcode":0,"answer_count":3,"duration_ms":21,"error":"","id":1}
```
