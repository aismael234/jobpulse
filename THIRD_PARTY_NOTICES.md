# Third-party notices

jobpulse ships one third-party file in its source tree. Its Python dependencies are installed
from PyPI under their own licenses (see `uv.lock`), and the Docker image also contains the
BAAI/bge-small-en-v1.5 embedding model (MIT), downloaded at build time.

## htmx 2.0.11

`src/jobpulse/web/static/htmx.min.js`, from the `htmx.org` npm package (license 0BSD). Its
LICENSE file:

    Zero-Clause BSD
    =============

    Permission to use, copy, modify, and/or distribute this software for
    any purpose with or without fee is hereby granted.

    THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL
    WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
    OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
    FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
    DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
    AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
    OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
