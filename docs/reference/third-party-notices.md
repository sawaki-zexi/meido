# 第三方代码与设计引用

状态：当前

最后核验：2026-10-15

多端会话模块参考并改编 [Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent)，固定基线为 `1c241a2627bbcc99124aef71abaee237ac057499`。引用范围包括渠道账号和身份、入站暂停缓冲、聊天上下文隔离、群旁听以及平台传输行为；具体来源见[源码研究](../research/shiori-multichannel-integration.md)。上游采用 MIT 许可证，保留其版权及许可声明如下。

```text
MIT License

Copyright (c) 2026 YinFengWindy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
