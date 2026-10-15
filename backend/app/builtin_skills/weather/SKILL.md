---
id: weather
version: 1.0.0
description: 查询当前天气和预报。触发词：天气、温度、weather、forecast。
tools: [shell]
activation: explicit
requires.bins: [curl]
---
# 天气查询

先确认 Shell 已获准且 curl 在命令白名单。Shell 接受 `{"command":"..."}`，按 argv 执行；不支持管道、变量展开、重定向或用户指定环境。

优先查询 wttr.in（无需 API key）：

```text
curl --fail --silent --show-error --location --max-time 15 "https://wttr.in/London?format=3"
curl --fail --silent --show-error --location --max-time 15 "https://wttr.in/London?format=%l:+%c+%t+%h+%w"
curl --fail --silent --show-error --location --max-time 15 "https://wttr.in/London?T"
```

把 London 换成已确认城市或机场代码；空格需 URL 编码，不能插入 Shell 操作符。`?m` 公制，`?u` 英制，`?0` 当前，`?1` 今天。
保留返回地点、时间、单位与来源；超时、非零退出、空正文不能说查询成功。

wttr.in 失败时，可用 Open-Meteo JSON 备用服务（同样无需 key）。先明确城市经纬度，不能编造：

```text
curl --fail --silent --show-error --location --max-time 15 "https://geocoding-api.open-meteo.com/v1/search?name=London&count=1&language=zh&format=json"
curl --fail --silent --show-error --location --max-time 15 "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.12&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code&timezone=auto"
```

使用 geocoding 的实际坐标；说明备用来源。两者都失败时报告具体失败，不根据常识猜当前天气。
缺少 curl 或 Shell 授权时，说明需安装/配置；技能激活不会修改白名单。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/weather/SKILL.md（MIT）。服务说明：https://wttr.in/:help 与 https://open-meteo.com/en/docs 。
