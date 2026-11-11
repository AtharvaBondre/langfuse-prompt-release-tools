# Third-party notices

`compose.demo.json` is derived from Langfuse's [Docker Compose configuration](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/docker-compose.yml) at commit `e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3`. Changes pin image digests, require freshly generated credentials, restrict published ports, disable telemetry and adjust local resource limits.

The upstream [license notice](https://github.com/langfuse/langfuse/blob/e3a2bee2a2c048adf29b8d8a18b29bd4a8bf8ac3/LICENSE) is reproduced below. This package does not redistribute or relicense the referenced container images; each image remains subject to its own applicable licenses.

```text
Copyright (c) 2023-2026 ClickHouse, Inc.

Portions of this software are licensed as follows:

- All content that resides under the "ee/", "web/src/ee/", and/or "worker/src/ee/" directories of this repository, if these directories exist, is licensed under the license defined in "ee/LICENSE".
- All third party components incorporated into the Langfuse Software are licensed under the original license provided by the owner of the applicable component.
- Content outside of the above mentioned directories or restrictions above is available under the "MIT Expat" license as defined below.

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
