# JavLibrary 网络适配

本分支基于上游 `c4cfe61188234dd24c75b53b42b054327fef3e58`，使用 YAML 配置。
已经发布的 v1.8 EXE 使用 INI 配置，不能直接加载本分支的 Python 文件或 YAML 设置。

## 为什么旧实现失败

1. 镜像域名会轮换。旧配置中的 `y78k.com` 在本次测试中连接失败。
2. 镜像发现依赖 GitHub 简介节点完整的 CSS class 字符串。GitHub 页面结构改变后，
   XPath 找不到节点，外围异常处理又把失败变成空字符串，因而继续使用旧地址。
3. 初始化跳过主域名直连测试，并只检查首页 HTTP 200，不能确认搜索功能可用。
4. `proxies={}` 仍可能继承系统/环境代理，不等于真正直连。
5. `cloudscraper` 失败后退回普通请求不会自动解决现代 Cloudflare 验证；
   403 随后进入通用重试循环，同一番号会迅速重复请求。

上游也有同类报告：[issue 488](https://github.com/Yuukiy/JavSP/issues/488)、
[issue 559](https://github.com/Yuukiy/JavSP/issues/559)。这些报告用于对照，下面的结果来自本地实测。

## 本分支的行为

- 使用[官方 GitHub 账号 API](https://api.github.com/users/javlibcom)读取镜像简介，
  支持 `== 域名前缀 ==` 和完整 HTTPS URL；发现失败时仍测试配置地址与主域名。
- `auto` 先测试直连，再测试配置的代理。也可指定 `direct` 或 `proxy`。
  这些设置只影响 JavLibrary；不会改变其他来源的代理配置。
- 用实际搜索请求选择路线，检查状态码、Cloudflare 标记和 JavLibrary 页面结构。
  详情页是否存在由 DOM 判断，支持详情页直接返回以及搜索页发生重定向。
- 每个站点和路线各自保留 Session，复用连接和 Cookie，避免跨路线共享会话。
  直连 Session 明确禁用环境代理，TLS 证书校验保持开启。
- 默认请求间隔 2 秒。429 立即停止并遵守 `Retry-After`；已选路线遇到验证或连接失败，
  默认冷却 300 秒，后续影片不再立刻重复请求。冷却结束后重新发现镜像。
- 网络错误只报告站点、路线和错误类别，不输出代理凭据或 Cookie。

它依靠当前可访问的官方镜像和正确的网络路线取得数据。若所有路线都要求验证，
仍会报告 `SiteBlocked`，不保证自动完成 Cloudflare 验证。

## 配置

在现有 `config.yml` 的 `network` 节点中加入以下字段。旧 YAML 文件可省略整个
`javlib` 节点，默认使用 `auto`。

```yaml
network:
  # 保留原有 proxy_server、proxy_free、timeout、retry 等配置
  javlib:
    mode: direct
    base_url: https://www.c99i.com
    discover_mirror: true
    minimum_interval: 2
    cooldown: 300
```

`c99i.com` 是 2026-09-30 实测地址，可能再次轮换。将 `base_url` 设为 `null` 可由
配置镜像和官方发现结果决定；新发现的镜像仍需通过实际搜索页检查。
`mode: proxy` 要求配置 `network.proxy_server`。

## 先运行只读探测

按项目原有方式安装依赖（Python 3.10–3.12、Poetry），从仓库根目录运行：

```powershell
poetry install
poetry run python -m javsp.javlib_probe IPX-177 SSNI-589
# 使用单独的 YAML 配置
poetry run python -m javsp.javlib_probe -c path/to/config.yml IPX-177
```

探测仅请求元数据 HTML，输出番号、字段是否存在、演员数量和选中路线，
不扫描媒体目录、不下载封面、不整理文件。全部成功时退出码为 0；
缺失、重复或网络错误时为 1。重复番号需要单独处理，不应当按反爬失败重试。

## 验证记录（2026-09-30）

| 请求方式 | 站点 | 结果 |
| --- | --- | --- |
| 普通请求，直连 | 主域名 | 403，`cf-mitigated: challenge` |
| 普通请求，经本机现有代理 | 主域名 | 403，Cloudflare 验证 |
| 普通请求，直连 | 旧镜像 y78k.com | 连接失败 |
| 普通请求，直连 | 官方当前镜像 c99i.com | 搜索、详情页 200 |
| 普通请求，经本机现有代理 | c99i.com | 403，Cloudflare 验证 |
| curl_cffi 0.14.0 / Chrome 指纹 | 主域名直连和代理 | 仍为 403 |

因此本分支没有引入 curl_cffi 依赖。本地定制配置选用镜像直连。

在同一 Session 中依次查询上游已有测试番号：

- `IPX-177`：取得匹配番号、标题、封面 URL、演员字段。
- `MIDV-001`：取得搜索页，识别出 3 个完全匹配结果，保留重复番号异常。
- `SSNI-589`：在上述重复结果后继续成功取得详情，复用了相同路线。

该三项探测的退出码为 1，原因是中间一项重复；不是三部均刮削成功。
本次没有测试完整整理流水线，也没有重新打包 EXE。

离线测试：

```powershell
python -m pytest unittest/test_javlib_network.py unittest/test_javlib_parser.py unittest/test_avid.py unittest/test_lib.py -q
```

结果：48 项通过（其中 39 项为新增 JavLibrary 测试）。覆盖代理隔离、会话复用、
自动路线选择、限速、冷却、探测预算、验证页、镜像发现、详情/搜索页和旧 YAML 兼容。
测试环境为 Windows / Python 3.12，requests 2.31.0、lxml 5.2.2。
