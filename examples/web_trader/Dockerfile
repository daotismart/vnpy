FROM python:3.12-slim-bookworm AS builder

ARG DEBIAN_MIRROR=

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    PIP_PREFER_BINARY=1

RUN set -eux; \
    if [ -n "$DEBIAN_MIRROR" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|http://deb.debian.org/debian|http://${DEBIAN_MIRROR}/debian|g; s|http://deb.debian.org/debian-security|http://${DEBIAN_MIRROR}/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends g++ make pkg-config ca-certificates; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-web.txt /app/requirements-web.txt
RUN pip install --no-cache-dir -r /app/requirements-web.txt
RUN pip install --no-cache-dir --no-deps \
        vnpy==4.4.0 \
        vnpy_ctp==6.7.11.2 \
        vnpy_ctastrategy==1.3.3 \
        vnpy_ctabacktester==1.2.0 \
        vnpy_datamanager==1.2.0 \
        vnpy_datarecorder==1.1.1 \
        vnpy_optionmaster==1.2.2 \
        vnpy_scripttrader==1.1.1 \
        vnpy_spreadtrading==1.3.1


FROM python:3.12-slim-bookworm

ARG DEBIAN_MIRROR=

ENV TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    LC_CTYPE=zh_CN.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QT_QPA_PLATFORM=offscreen \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/vnpy_ctp/api

RUN set -eux; \
    if [ -n "$DEBIAN_MIRROR" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|http://deb.debian.org/debian|http://${DEBIAN_MIRROR}/debian|g; s|http://deb.debian.org/debian-security|http://${DEBIAN_MIRROR}/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tzdata locales libnsl2 libatomic1 libgomp1 libstdc++6 libfreetype6 libpng16-16 libjpeg62-turbo; \
    sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/; s/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/; s/^# *zh_CN.GB18030 GB18030/zh_CN.GB18030 GB18030/' /etc/locale.gen; \
    grep -q '^zh_CN.GB18030 GB18030' /etc/locale.gen || echo 'zh_CN.GB18030 GB18030' >> /etc/locale.gen; \
    locale-gen; \
    localedef -c -f GB18030 -i zh_CN zh_CN.GB18030 || true; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
WORKDIR /app
COPY . /app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=8 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5)"

CMD ["python", "run_server.py"]
