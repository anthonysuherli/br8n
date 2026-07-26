# Third-Party Licenses — br8n backend

> Generated inventory of the Python dependencies br8n's backend resolves into.
> br8n itself is [MIT](../LICENSE); this file documents that its dependency
> tree is license-compatible with shipping under MIT.
>
> Regenerate with:
> ```bash
> cd backend && .venv/bin/pip install pip-licenses
> .venv/bin/pip-licenses --format=markdown --with-urls --order=license
> ```
> *Snapshot date: 2026-06-03. Covers the backend (`backend/`) runtime + dev deps
> as resolved in the local venv; the static `site/` has no bundled runtime deps.*

## Compatibility summary

| License family | Count | Compatible with MIT distribution? |
|---|---:|---|
| MIT | 58 | ✅ Yes |
| Apache-2.0 | 20 | ✅ Yes (permissive + patent grant) |
| BSD (2/3-clause) | 20 | ✅ Yes |
| MPL-2.0 | 3 | ✅ Yes — **file-level** copyleft only |
| PSF-2.0 | 1 | ✅ Yes |
| **GPL / AGPL / LGPL** | **0** | — none present |

**Total: 102 dependencies. No strong copyleft (GPL/AGPL/LGPL) anywhere.**

### Note on the MPL-2.0 dependencies (`certifi`, `orjson`, `tqdm`)

MPL-2.0 is *file-level* (weak) copyleft: it only requires sharing modifications
to the **MPL-licensed files themselves**, not to any project that merely depends
on them. br8n consumes these packages unmodified, so no copyleft obligation is
triggered, and br8n may ship under MIT without restriction. (`orjson` and
`tqdm` are additionally dual-licensed with permissive options.)

## Full inventory

| Package | Version | License | Source |
|---|---|---|---|
| multidict | 6.7.1 | Apache License 2.0 | [link](https://github.com/aio-libs/multidict) |
| deprecation | 2.1.0 | Apache Software License | [link](http://deprecation.readthedocs.io/) |
| distro | 1.9.0 | Apache Software License | [link](https://github.com/python-distro/distro) |
| openai | 2.38.0 | Apache Software License | [link](https://github.com/openai/openai-python) |
| propcache | 0.5.2 | Apache Software License | [link](https://github.com/aio-libs/propcache) |
| requests | 2.34.2 | Apache Software License | [link](https://github.com/psf/requests) |
| requests-toolbelt | 1.0.0 | Apache Software License | [link](https://toolbelt.readthedocs.io/) |
| tenacity | 9.1.4 | Apache Software License | [link](https://github.com/jd/tenacity) |
| python-dateutil | 2.9.0.post0 | Apache Software License; BSD License | [link](https://github.com/dateutil/dateutil) |
| sniffio | 1.3.1 | Apache Software License; MIT License | [link](https://github.com/python-trio/sniffio) |
| uvloop | 0.22.1 | Apache Software License; MIT License |  |
| asyncpg | 0.31.0 | Apache-2.0 |  |
| pyiceberg | 0.11.1 | Apache-2.0 | [link](https://py.iceberg.apache.org/) |
| pytest-asyncio | 1.4.0 | Apache-2.0 | [link](https://github.com/pytest-dev/pytest-asyncio) |
| python-multipart | 0.0.29 | Apache-2.0 | [link](https://github.com/Kludex/python-multipart) |
| yarl | 1.24.2 | Apache-2.0 | [link](https://github.com/aio-libs/yarl) |
| regex | 2026.5.9 | Apache-2.0 AND CNRI-Python | [link](https://github.com/mrabarnett/mrab-regex) |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | [link](https://github.com/pypa/packaging) |
| cryptography | 48.0.0 | Apache-2.0 OR BSD-3-Clause | [link](https://github.com/pyca/cryptography) |
| Jinja2 | 3.1.6 | BSD License | [link](https://github.com/pallets/jinja/) |
| httpx | 0.28.1 | BSD License | [link](https://github.com/encode/httpx) |
| jsonpatch | 1.33 | BSD License | [link](https://github.com/stefankoegl/python-json-patch) |
| jsonpointer | 3.1.1 | BSD License | [link](https://github.com/stefankoegl/python-json-pointer) |
| nodeenv | 1.10.0 | BSD License | [link](https://github.com/ekalinin/nodeenv) |
| websockets | 15.0.1 | BSD License | [link](https://github.com/python-websockets/websockets) |
| xxhash | 3.7.0 | BSD License | [link](https://github.com/ifduyue/python-xxhash) |
| Pygments | 2.20.0 | BSD-2-Clause | [link](https://pygments.org) |
| MarkupSafe | 3.0.3 | BSD-3-Clause | [link](https://github.com/pallets/markupsafe/) |
| click | 8.4.1 | BSD-3-Clause | [link](https://github.com/pallets/click/) |
| fsspec | 2026.4.0 | BSD-3-Clause | [link](https://github.com/fsspec/filesystem_spec) |
| httpcore | 1.0.9 | BSD-3-Clause | [link](https://www.encode.io/httpcore/) |
| idna | 3.17 | BSD-3-Clause | [link](https://github.com/kjd/idna) |
| pycparser | 3.0 | BSD-3-Clause | [link](https://github.com/eliben/pycparser) |
| python-dotenv | 1.2.2 | BSD-3-Clause | [link](https://github.com/theskumar/python-dotenv) |
| sse-starlette | 3.4.4 | BSD-3-Clause | [link](https://github.com/sysid/sse-starlette) |
| starlette | 1.2.0 | BSD-3-Clause | [link](https://github.com/Kludex/starlette) |
| uuid_utils | 0.16.0 | BSD-3-Clause | [link](https://github.com/aminalaee/uuid-utils) |
| uvicorn | 0.48.0 | BSD-3-Clause | [link](https://uvicorn.dev/) |
| zstandard | 0.25.0 | BSD-3-Clause | [link](https://github.com/indygreg/python-zstandard) |
| PyJWT | 2.13.0 | MIT | [link](https://github.com/jpadilla/pyjwt) |
| annotated-doc | 0.0.4 | MIT | [link](https://github.com/fastapi/annotated-doc) |
| anyio | 4.13.0 | MIT | [link](https://anyio.readthedocs.io/en/stable/versionhistory.html) |
| argon2-cffi | 25.1.0 | MIT | [link](https://github.com/hynek/argon2-cffi/blob/main/CHANGELOG.md) |
| argon2-cffi-bindings | 25.1.0 | MIT | [link](https://github.com/hynek/argon2-cffi-bindings/blob/main/CHANGELOG.md) |
| attrs | 26.1.0 | MIT | [link](https://www.attrs.org/en/stable/changelog.html) |
| cachetools | 6.2.6 | MIT | [link](https://github.com/tkem/cachetools/) |
| cffi | 2.0.0 | MIT | [link](https://cffi.readthedocs.io/en/latest/whatsnew.html) |
| charset-normalizer | 3.4.7 | MIT | [link](https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md) |
| fastapi | 0.136.3 | MIT | [link](https://github.com/fastapi/fastapi) |
| httptools | 0.8.0 | MIT | [link](https://github.com/MagicStack/httptools) |
| httpx-sse | 0.4.3 | MIT | [link](https://github.com/florimondmanca/httpx-sse) |
| iniconfig | 2.3.0 | MIT | [link](https://github.com/pytest-dev/iniconfig) |
| jiter | 0.15.0 | MIT | [link](https://github.com/pydantic/jiter/) |
| jsonschema | 4.26.0 | MIT | [link](https://github.com/python-jsonschema/jsonschema) |
| jsonschema-specifications | 2025.9.1 | MIT | [link](https://github.com/python-jsonschema/jsonschema-specifications) |
| langsmith | 0.8.7 | MIT | [link](https://smith.langchain.com/) |
| postgrest | 2.30.1 | MIT | [link](https://github.com/supabase/supabase/tree/main/src/postgrest) |
| pydantic | 2.13.4 | MIT | [link](https://github.com/pydantic/pydantic) |
| pydantic-settings | 2.14.1 | MIT | [link](https://github.com/pydantic/pydantic-settings) |
| pydantic_core | 2.46.4 | MIT | [link](https://github.com/pydantic) |
| pyparsing | 3.3.2 | MIT | [link](https://github.com/pyparsing/pyparsing/) |
| pyright | 1.1.409 | MIT | [link](https://github.com/RobertCraigie/pyright-python) |
| pytest | 9.0.3 | MIT | [link](https://docs.pytest.org/en/latest/) |
| realtime | 2.30.1 | MIT | [link](https://github.com/supabase/supabase/tree/main/src/realime) |
| referencing | 0.37.0 | MIT | [link](https://github.com/python-jsonschema/referencing) |
| rpds-py | 2026.5.1 | MIT | [link](https://github.com/crate-py/rpds) |
| ruff | 0.15.15 | MIT | [link](https://docs.astral.sh/ruff) |
| storage3 | 2.30.1 | MIT | [link](https://supabase.github.io/storage-py) |
| supabase | 2.30.1 | MIT | [link](https://github.com/supabase/supabase-py) |
| supabase-auth | 2.30.1 | MIT | [link](https://github.com/supabase/supabase-py/tree/main/src/auth) |
| supabase-functions | 2.30.1 | MIT | [link](https://github.com/supabase/supabase-py/tree/main/src/functions) |
| typing-inspection | 0.4.2 | MIT | [link](https://github.com/pydantic/typing-inspection) |
| urllib3 | 2.7.0 | MIT | [link](https://github.com/urllib3/urllib3/blob/main/CHANGES.rst) |
| PyYAML | 6.0.3 | MIT License | [link](https://pyyaml.org/) |
| StrEnum | 0.4.15 | MIT License | [link](https://github.com/irgeek/StrEnum) |
| annotated-types | 0.7.0 | MIT License | [link](https://github.com/annotated-types/annotated-types) |
| anthropic | 0.105.2 | MIT License | [link](https://github.com/anthropics/anthropic-sdk-python) |
| docstring_parser | 0.18.0 | MIT License | [link](https://github.com/rr-/docstring_parser) |
| h11 | 0.16.0 | MIT License | [link](https://github.com/python-hyper/h11) |
| h2 | 4.3.0 | MIT License | [link](https://github.com/python-hyper/h2/) |
| hpack | 4.1.0 | MIT License | [link](https://github.com/python-hyper/hpack/) |
| hyperframe | 6.1.0 | MIT License | [link](https://github.com/python-hyper/hyperframe/) |
| langchain-anthropic | 1.4.4 | MIT License | [link](https://docs.langchain.com/oss/python/integrations/providers/anthropic) |
| langchain-core | 1.4.0 | MIT License | [link](https://docs.langchain.com/) |
| langchain-protocol | 0.0.16 | MIT License | [link](https://github.com/langchain-ai/agent-protocol/tree/main/streaming) |
| markdown-it-py | 4.2.0 | MIT License | [link](https://github.com/executablebooks/markdown-it-py) |
| mcp | 1.27.2 | MIT License | [link](https://modelcontextprotocol.io) |
| mdurl | 0.1.2 | MIT License | [link](https://github.com/executablebooks/mdurl) |
| mmh3 | 5.2.1 | MIT License | [link](https://pypi.org/project/mmh3/) |
| pluggy | 1.6.0 | MIT License |  |
| pyroaring | 1.1.0 | MIT License | [link](https://github.com/Ezibenroc/PyRoaringBitMap) |
| rich | 14.3.4 | MIT License | [link](https://github.com/Textualize/rich) |
| six | 1.17.0 | MIT License | [link](https://github.com/benjaminp/six) |
| strictyaml | 1.7.3 | MIT License | [link](https://hitchdev.com/strictyaml) |
| tavily-python | 0.7.25 | MIT License | [link](https://github.com/tavily-ai/tavily-python) |
| watchfiles | 1.2.0 | MIT License | [link](https://github.com/samuelcolvin/watchfiles) |
| tiktoken | 0.13.0 | MIT License Copyright (c) 2022 OpenAI, Shantanu Jain Permiss | [link](https://github.com/openai/tiktoken) |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 | [link](https://github.com/asg017/sqlite-vec) |
| orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) | [link](https://github.com/ijl/orjson) |
| tqdm | 4.67.3 | MPL-2.0 AND MIT | [link](https://tqdm.github.io) |
| certifi | 2026.5.20 | Mozilla Public License 2.0 (MPL 2.0) | [link](https://github.com/certifi/python-certifi) |
| typing_extensions | 4.15.0 | PSF-2.0 | [link](https://github.com/python/typing_extensions) |
