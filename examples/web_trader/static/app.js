const TOKEN_KEY = "web_trader_token";
const state = {
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    socket: null,
    ticks: {},
    orders: {},
    trades: {},
    positions: {},
    accounts: {},
    strategies: {},
    spreadStrategies: {},
    spreads: {},
    futuresProducts: [],
    futuresCurve: null,
    futuresProductKey: "",
    scriptBacktest: null,
    scriptBtPresets: [],
    scriptBtPresetKey: "",
    liveMonitor: null,
    meta: { exchanges: [], intervals: [], directions: [], offsets: [], order_types: [], option_models: [] },
};

const SCRIPT_FILE_FALLBACK = [
    { value: "gex_tv_strangle.py", label: "gex_tv_strangle.py  ·  GEX 铁鹰" },
    { value: "io_covered_call.py", label: "io_covered_call.py  ·  备兑看涨" },
    { value: "as_option_mm.py", label: "as_option_mm.py  ·  AS 做市" },
    { value: "sa_cta_trend.py", label: "sa_cta_trend.py  ·  SA 趋势" },
];
const GEX_FALLBACK_PRESETS = [
    { name: "原优化6%", risk_cap: 0.06, roll_dte: 21, max_lots: 80, iv_rank_min: 40 },
    { name: "进取20%", risk_cap: 0.20, roll_dte: 21, max_lots: 250, iv_rank_min: 40 },
    { name: "容量80手", risk_cap: 0.46, roll_dte: 21, max_lots: 80, iv_rank_min: 40 },
];
const AS_FALLBACK_PRESETS = [
    {
        name: "实盘默认参数",
        gamma: 0.08,
        kappa: 1.4,
        spread_mult: 0.02,
        sigma_floor: 0.18,
        tau_days: 0.15,
        max_pos: 10,
        hedge: true,
    },
];

function $(id) {
    return document.getElementById(id);
}

function appendLog(msg) {
    const box = $("log-box");
    box.textContent += `${msg}\n`;
    box.scrollTop = box.scrollHeight;
}

function forceLogin(message) {
    sessionStorage.removeItem(TOKEN_KEY);
    state.token = "";
    if ($("app-page")) {
        $("app-page").classList.add("hidden");
    }
    if ($("login-page")) {
        $("login-page").classList.remove("hidden");
    }
    if ($("login-error") && message) {
        $("login-error").textContent = message;
    }
}

async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (state.token) {
        headers.Authorization = `Bearer ${state.token}`;
    }
    if (options.json) {
        headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(options.json);
    }
    const response = await fetch(path, Object.assign({}, options, { headers }));
    if (response.status === 401) {
        forceLogin("登录已过期，请重新登录后再回测");
        throw new Error("登录已过期，请重新登录");
    }
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
        const detail = data && data.detail ? data.detail : data;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
}

function scriptFileName(value) {
    return String(value || "").split(/[/\\]/).pop();
}

function scriptFileLabel(value) {
    const name = scriptFileName(value);
    const found = SCRIPT_FILE_FALLBACK.find((item) => item.value === name);
    return found ? found.label : name;
}

function fillScriptFileSelect(files, selected) {
    const list = $("script-file-list");
    const hidden = $("script-file");
    if (!list || !hidden) {
        return;
    }
    const items = (files && files.length)
        ? files.map((file) => ({ value: file, label: scriptFileLabel(file) }))
        : SCRIPT_FILE_FALLBACK.slice();
    const preferName = scriptFileName(selected || hidden.value);
    const preferred = items.find((item) => item.value === selected)
        || items.find((item) => scriptFileName(item.value) === preferName)
        || items.find((item) => scriptFileName(item.value) === "gex_tv_strangle.py")
        || items[0];
    list.innerHTML = "";
    items.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "script-file-item";
        button.dataset.path = item.value;
        button.textContent = item.label;
        if (preferred && item.value === preferred.value) {
            button.classList.add("active");
            hidden.value = item.value;
        }
        list.appendChild(button);
    });
    if (preferred) {
        hidden.value = preferred.value;
    }
}

function fillSelect(select, values, selected) {
    if (!select || !values || !values.length) {
        return;
    }
    const current = selected || select.value;
    select.innerHTML = "";
    values.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = select.id === "script-file"
            ? String(value).split(/[/\\]/).pop()
            : value;
        if (value === current) {
            option.selected = true;
        }
        select.appendChild(option);
    });
}

function renderTable(bodyId, rows, htmlFn) {
    $(bodyId).innerHTML = rows.map(htmlFn).join("");
}

function sideClass(value) {
    const text = String(value || "");
    if (text.includes("多") || text.includes("long")) {
        return "buy";
    }
    if (text.includes("空") || text.includes("short")) {
        return "sell";
    }
    return "";
}

let wsRetry = 0;
function connectWs() {
    if (state.socket) {
        state.socket.onclose = null;
        state.socket.close();
        state.socket = null;
    }
    if (!state.token) {
        return;
    }
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    state.socket = new WebSocket(`${protocol}://${location.host}/ws/?token=${state.token}`);
    state.socket.onopen = () => {
        wsRetry = 0;
        $("ws-status").textContent = "推送已连接";
        $("ws-status").classList.add("on");
    };
    state.socket.onclose = () => {
        $("ws-status").textContent = "推送断开";
        $("ws-status").classList.remove("on");
        if (!state.token) {
            return;
        }
        wsRetry = Math.min(wsRetry + 1, 8);
        setTimeout(connectWs, 1000 * wsRetry);
    };
    state.socket.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        handleEvent(payload.topic, payload.data);
    };
}

function handleEvent(topic, data) {
    if (topic === "eTick.") {
        if (data && data.vt_symbol) {
            state.ticks[data.vt_symbol] = data;
            scheduleTickRender();
        }
        scheduleChainRefresh();
        scheduleFuturesCurveRefresh();
    } else if (topic === "eContract.") {
        scheduleFuturesProductsRefresh();
    } else if (topic === "eOrder.") {
        state.orders[data.vt_orderid] = data;
        renderOrders();
    } else if (topic === "eTrade.") {
        state.trades[data.vt_tradeid] = data;
        renderTrades();
        scheduleChainRefresh();
    } else if (topic === "ePosition.") {
        state.positions[data.vt_positionid] = data;
        renderPositions();
        scheduleChainRefresh();
    } else if (topic === "eAccount.") {
        state.accounts[data.vt_accountid] = data;
        renderAccounts();
    } else if (topic === "eCtaStrategy") {
        state.strategies[data.strategy_name] = data;
        renderStrategies();
    } else if (topic === "eSpreadData" || topic === "eSpreadPos") {
        state.spreads[data.name] = data;
        renderSpreads();
    } else if (topic === "eSpreadStrategy") {
        state.spreadStrategies[data.strategy_name] = data;
        renderSpreadStrategies();
    } else if (topic === "eLog" || topic === "eCtaLog" || topic === "eBacktesterLog" || topic === "eSpreadLog" || topic === "eScriptLog" || topic === "eOptionAlgoLog" || topic === "eRecorderLog") {
        appendLog(`[${data.time || ""}] ${data.msg || ""}`);
    } else if (topic === "eRecorderUpdate") {
        renderRecorder(data);
    } else if (topic === "eBacktesterBacktestingFinished") {
        appendLog("回测完成");
        loadBacktestResult();
    } else if (topic === "eOptionNewPortfolio") {
        scheduleOptionRefresh();
    }
}

let tickRenderTimer = null;
function scheduleTickRender() {
    if (tickRenderTimer) {
        return;
    }
    tickRenderTimer = setTimeout(() => {
        tickRenderTimer = null;
        renderTicks();
    }, 300);
}

function renderTicks() {
    renderTable("tick-body", Object.values(state.ticks), (item) => `
        <tr>
            <td>${item.vt_symbol || ""}</td>
            <td>${item.last_price ?? ""}</td>
            <td>${item.bid_price_1 ?? ""}</td>
            <td>${item.ask_price_1 ?? ""}</td>
            <td>${item.volume ?? ""}</td>
        </tr>`);
}

function renderAccounts() {
    renderTable("account-body", Object.values(state.accounts), (item) => `
        <tr>
            <td>${item.accountid || item.vt_accountid || ""}</td>
            <td>${item.balance ?? ""}</td>
            <td>${item.frozen ?? ""}</td>
            <td>${item.available ?? ""}</td>
        </tr>`);
}

function renderPositions() {
    renderTable("position-body", Object.values(state.positions), (item) => `
        <tr>
            <td>${item.vt_symbol || ""}</td>
            <td class="${sideClass(item.direction)}">${item.direction || ""}</td>
            <td>${item.volume ?? ""}</td>
            <td>${item.price ?? ""}</td>
            <td>${item.pnl ?? ""}</td>
        </tr>`);
}

function renderOrders() {
    renderTable("order-body", Object.values(state.orders), (item) => `
        <tr>
            <td>${item.vt_orderid || ""}</td>
            <td>${item.vt_symbol || ""}</td>
            <td class="${sideClass(item.direction)}">${item.direction || ""}</td>
            <td>${item.offset || ""}</td>
            <td>${item.price ?? ""}</td>
            <td>${item.volume ?? ""}</td>
            <td>${item.status || ""}</td>
            <td><button class="small ghost" data-cancel="${item.vt_orderid}">撤单</button></td>
        </tr>`);
}

function renderTrades() {
    renderTable("trade-body", Object.values(state.trades), (item) => `
        <tr>
            <td>${item.vt_tradeid || ""}</td>
            <td>${item.vt_symbol || ""}</td>
            <td class="${sideClass(item.direction)}">${item.direction || ""}</td>
            <td>${item.price ?? ""}</td>
            <td>${item.volume ?? ""}</td>
            <td>${item.datetime || ""}</td>
        </tr>`);
}

function renderStrategies() {
    renderTable("cta-body", Object.values(state.strategies), (item) => `
        <tr>
            <td>${item.strategy_name || ""}</td>
            <td>${item.class_name || ""}</td>
            <td>${item.vt_symbol || ""}</td>
            <td>${item.variables && item.variables.inited ? "是" : "否"}</td>
            <td>${item.variables && item.variables.trading ? "是" : "否"}</td>
            <td>${JSON.stringify(item.variables || {})}</td>
            <td>
                <button class="small ok" data-cta="init" data-name="${item.strategy_name}">初始化</button>
                <button class="small" data-cta="start" data-name="${item.strategy_name}">启动</button>
                <button class="small ghost" data-cta="stop" data-name="${item.strategy_name}">停止</button>
                <button class="small danger" data-cta="remove" data-name="${item.strategy_name}">删除</button>
            </td>
        </tr>`);
}

function paramFields(containerId, params) {
    const box = $(containerId);
    box.innerHTML = "";
    Object.entries(params || {}).forEach(([key, value]) => {
        const label = document.createElement("label");
        label.textContent = key;
        const input = document.createElement("input");
        input.dataset.param = key;
        input.value = value;
        label.appendChild(input);
        box.appendChild(label);
    });
}

function readParams(containerId) {
    const params = {};
    $(containerId).querySelectorAll("input[data-param]").forEach((input) => {
        const raw = input.value;
        params[input.dataset.param] = raw === "" || Number.isNaN(Number(raw)) ? raw : Number(raw);
    });
    return params;
}

async function loadGatewayForm() {
    const data = await api("/gateway");
    const box = $("gateway-form");
    box.innerHTML = "";
    const gateway = data.gateways[0];
    if (!gateway) {
        return;
    }
    const nameInput = document.createElement("input");
    nameInput.type = "hidden";
    nameInput.id = "gateway-name";
    nameInput.value = gateway.name;
    box.appendChild(nameInput);

    Object.entries(gateway.fields).forEach(([key, value]) => {
        const label = document.createElement("label");
        label.textContent = key;
        if (gateway.choices[key]) {
            const select = document.createElement("select");
            select.dataset.field = key;
            fillSelect(select, gateway.choices[key], value);
            label.appendChild(select);
        } else {
            const input = document.createElement("input");
            input.dataset.field = key;
            input.value = value || "";
            if (key.includes("密") || key.includes("授权")) {
                input.type = "password";
                input.placeholder = gateway.password_saved ? "已保存，留空则沿用" : "";
            }
            label.appendChild(input);
        }
        box.appendChild(label);
    });
}

async function loadMeta() {
    state.meta = await api("/meta");
    fillSelect($("order-exchange"), state.meta.exchanges, "SHFE");
    fillSelect($("order-direction"), state.meta.directions, "多");
    fillSelect($("order-offset"), state.meta.offsets, "开");
    fillSelect($("order-type"), state.meta.order_types, "限价");
    fillSelect($("bt-interval"), state.meta.intervals, "1m");
    fillSelect($("data-exchange"), state.meta.exchanges, "SHFE");
    fillSelect($("data-interval"), state.meta.intervals, "1m");
    fillSelect($("opt-model"), state.meta.option_models || []);
    fillSelect($("sp-algo-dir"), state.meta.directions, "多");
}

async function refreshTrading() {
    const [ticks, orders, trades, positions, accounts, logs] = await Promise.all([
        api("/tick"),
        api("/order"),
        api("/trade"),
        api("/position"),
        api("/account"),
        api("/log"),
    ]);
    ticks.forEach((item) => { state.ticks[item.vt_symbol] = item; });
    orders.forEach((item) => { state.orders[item.vt_orderid] = item; });
    trades.forEach((item) => { state.trades[item.vt_tradeid] = item; });
    positions.forEach((item) => { state.positions[item.vt_positionid] = item; });
    accounts.forEach((item) => { state.accounts[item.vt_accountid] = item; });
    renderTicks();
    renderOrders();
    renderTrades();
    renderPositions();
    renderAccounts();
    logs.forEach((item) => appendLog(`[${item.time}] ${item.msg}`));
}

async function refreshCta() {
    const classes = await api("/cta/class");
    fillSelect($("cta-class"), classes);
    if (classes[0]) {
        paramFields("cta-params", await api(`/cta/class/${classes[0]}`));
    }
    const strategies = await api("/cta/strategy");
    strategies.forEach((item) => { state.strategies[item.strategy_name] = item; });
    renderStrategies();
}

async function refreshBacktestClasses() {
    const classes = await api("/backtest/class");
    fillSelect($("bt-class"), classes);
    if (classes[0]) {
        paramFields("bt-params", await api(`/backtest/class/${classes[0]}`));
    }
}

async function refreshData() {
    const overview = await api("/data/overview");
    renderTable("data-body", overview, (item) => `
        <tr>
            <td>${item.symbol || ""}</td>
            <td>${item.exchange || ""}</td>
            <td>${item.interval || ""}</td>
            <td>${item.count ?? ""}</td>
            <td>${item.start || ""}</td>
            <td>${item.end || ""}</td>
            <td>
                <button class="small ghost" data-data="export" data-symbol="${item.symbol}" data-exchange="${item.exchange}" data-interval="${item.interval}" data-start="${item.start || ""}" data-end="${item.end || ""}">导出</button>
                <button class="small danger" data-data="delete" data-symbol="${item.symbol}" data-exchange="${item.exchange}" data-interval="${item.interval}">删除</button>
            </td>
        </tr>`);
    if ($("tick-data-body")) {
        const ticks = await api("/data/tick/overview");
        renderTable("tick-data-body", ticks, (item) => `
            <tr>
                <td>${item.symbol || ""}</td>
                <td>${item.exchange || ""}</td>
                <td>${item.count ?? ""}</td>
                <td>${item.start || ""}</td>
                <td>${item.end || ""}</td>
                <td>
                    <button class="small ghost" data-tick="export" data-symbol="${item.symbol}" data-exchange="${item.exchange}" data-start="${item.start || ""}" data-end="${item.end || ""}">导出</button>
                    <button class="small danger" data-tick="delete" data-symbol="${item.symbol}" data-exchange="${item.exchange}">删除</button>
                </td>
            </tr>`);
    }
}

function renderRecorder(data) {
    const ticks = (data && data.tick) || [];
    const bars = (data && data.bar) || [];
    renderTable("rec-tick-body", ticks, (vtSymbol) => `
        <tr>
            <td>${vtSymbol}</td>
            <td><button class="small danger" data-rec="remove" data-kind="tick" data-symbol="${vtSymbol}">移除</button></td>
        </tr>`);
    renderTable("rec-bar-body", bars, (vtSymbol) => `
        <tr>
            <td>${vtSymbol}</td>
            <td><button class="small danger" data-rec="remove" data-kind="bar" data-symbol="${vtSymbol}">移除</button></td>
        </tr>`);
    const status = $("rec-status");
    if (status) {
        const pending = data && data.pending != null ? data.pending : "";
        const interval = data && data.interval_sec ? data.interval_sec : 10;
        const maxChains = data && data.max_chains != null ? data.max_chains : "";
        const auto = data && data.record_ticks ? "自动录制开" : "自动录制关";
        const scope = (data && data.scope) || (maxChains === 0 || maxChains === "0" ? "全部到期月" : (maxChains === "" ? "" : `近${maxChains}月`));
        const barOn = data && data.record_bar ? "Tick+K线" : "Tick";
        status.textContent = `${barOn} ${ticks.length}/${bars.length} ｜ ${interval} 秒写入一次${pending === "" ? "" : ` ｜ 待写入 ${pending}`} ｜ ${auto}${scope ? ` ${scope}` : ""}`;
    }
}

async function refreshRecorder() {
    if (!$("rec-tick-body")) {
        return;
    }
    try {
        renderRecorder(await api("/recorder"));
    } catch (error) {
        appendLog(error.message);
    }
}

async function loadBacktestResult() {
    const result = await api("/backtest/result");
    $("bt-stats").textContent = JSON.stringify(result.statistics, null, 2);
    const trades = await api("/backtest/trade");
    renderTable("bt-trade-body", trades, (item) => `
        <tr>
            <td>${item.datetime || ""}</td>
            <td class="${sideClass(item.direction)}">${item.direction || ""}</td>
            <td>${item.price ?? ""}</td>
            <td>${item.volume ?? ""}</td>
        </tr>`);
    const orders = await api("/backtest/order");
    renderTable("bt-order-body", orders, (item) => `
        <tr>
            <td>${item.datetime || ""}</td>
            <td class="${sideClass(item.direction)}">${item.direction || ""}</td>
            <td>${item.price ?? ""}</td>
            <td>${item.volume ?? ""}</td>
            <td>${item.status || ""}</td>
        </tr>`);
}

async function afterLogin() {
    await api("/script");
    if ($("login-error")) {
        $("login-error").textContent = "";
    }
    $("login-page").classList.add("hidden");
    $("app-page").classList.remove("hidden");
    const steps = [
        loadMeta,
        loadGatewayForm,
        refreshTrading,
        refreshCta,
        refreshOption,
        refreshFuturesProducts,
        refreshSpread,
        refreshLive,
        refreshScript,
        refreshScriptBacktest,
        refreshBacktestClasses,
        refreshData,
        refreshRecorder,
    ];
    for (const step of steps) {
        try {
            await step();
        } catch (error) {
            appendLog(error.message);
        }
    }
    connectWs();
    startOptionPoll();
}

$("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-error").textContent = "";
    const body = new URLSearchParams();
    body.set("username", $("login-username").value);
    body.set("password", $("login-password").value);
    try {
        const data = await api("/token", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body,
        });
        state.token = data.access_token;
        sessionStorage.setItem(TOKEN_KEY, data.access_token);
        await afterLogin();
    } catch (error) {
        $("login-error").textContent = error.message;
    }
});

$("logout-btn").addEventListener("click", () => {
    sessionStorage.removeItem(TOKEN_KEY);
    state.token = "";
    if (state.socket) {
        state.socket.onclose = null;
        state.socket.close();
        state.socket = null;
    }
    location.reload();
});

document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        $(`tab-${button.dataset.tab}`).classList.add("active");
        if (button.dataset.tab === "option") {
            startOptionPoll();
            if (lastOptionChain) {
                renderGexChart(lastOptionChain.gex || {});
                renderTvYieldChart(lastOptionChain);
                renderIvSmileChart(lastOptionChain);
            }
            refreshOptionChain().catch((error) => appendLog(error.message));
        }
        if (button.dataset.tab === "futures" && state.futuresCurve) {
            renderFuturesCharts(state.futuresCurve);
            renderFuturesCapitalCharts(state.futuresCurve);
        }
        if (button.dataset.tab === "live") {
            scheduleLiveMonitor();
            refreshLive().catch((error) => appendLog(error.message));
        }
        if (button.dataset.tab === "script") {
            scheduleScriptMonitor();
            refreshScriptBacktest().then(() => {
                if (state.scriptBacktest && state.scriptBacktest.running) {
                    scheduleScriptBtPoll();
                }
            }).catch((error) => appendLog(error.message));
        }
        if (button.dataset.tab === "data") {
            refreshRecorder();
            refreshData();
        }
    });
});

$("connect-btn").addEventListener("click", async () => {
    const setting = {};
    $("gateway-form").querySelectorAll("[data-field]").forEach((input) => {
        const value = input.value;
        if (value === "") {
            return;
        }
        setting[input.dataset.field] = value;
    });
    try {
        const result = await api("/gateway/connect", {
            method: "POST",
            json: { gateway_name: $("gateway-name").value, setting },
        });
        appendLog(result.message);
    } catch (error) {
        appendLog(error.message);
    }
});

$("subscribe-btn").addEventListener("click", async () => {
    try {
        const result = await api(`/tick/${encodeURIComponent($("subscribe-symbol").value)}`, { method: "POST" });
        appendLog(result.message);
    } catch (error) {
        appendLog(error.message);
    }
});

$("send-order-btn").addEventListener("click", async () => {
    try {
        const result = await api("/order", {
            method: "POST",
            json: {
                symbol: $("order-symbol").value,
                exchange: $("order-exchange").value,
                direction: $("order-direction").value,
                offset: $("order-offset").value,
                type: $("order-type").value,
                price: Number($("order-price").value),
                volume: Number($("order-volume").value),
            },
        });
        appendLog(`下单成功 ${result.vt_orderid}`);
    } catch (error) {
        appendLog(error.message);
    }
});

$("order-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel]");
    if (!button) {
        return;
    }
    try {
        const result = await api(`/order/${encodeURIComponent(button.dataset.cancel)}`, { method: "DELETE" });
        appendLog(result.message);
    } catch (error) {
        appendLog(error.message);
    }
});

$("cta-class").addEventListener("change", async () => {
    paramFields("cta-params", await api(`/cta/class/${$("cta-class").value}`));
});

$("cta-add-btn").addEventListener("click", async () => {
    try {
        const result = await api("/cta/strategy", {
            method: "POST",
            json: {
                class_name: $("cta-class").value,
                strategy_name: $("cta-name").value,
                vt_symbol: $("cta-symbol").value,
                setting: readParams("cta-params"),
            },
        });
        appendLog(result.message);
        await refreshCta();
    } catch (error) {
        appendLog(error.message);
    }
});

$("cta-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cta]");
    if (!button) {
        return;
    }
    const name = button.dataset.name;
    const action = button.dataset.cta;
    try {
        if (action === "remove") {
            await api(`/cta/strategy/${encodeURIComponent(name)}`, { method: "DELETE" });
            delete state.strategies[name];
            renderStrategies();
        } else {
            await api(`/cta/strategy/${encodeURIComponent(name)}/${action}`, { method: "POST" });
        }
        await refreshCta();
    } catch (error) {
        appendLog(error.message);
    }
});

$("bt-class").addEventListener("change", async () => {
    paramFields("bt-params", await api(`/backtest/class/${$("bt-class").value}`));
});

$("bt-start-btn").addEventListener("click", async () => {
    try {
        const result = await api("/backtest", {
            method: "POST",
            json: {
                class_name: $("bt-class").value,
                vt_symbol: $("bt-symbol").value,
                interval: $("bt-interval").value,
                start: $("bt-start").value,
                end: $("bt-end").value,
                rate: Number($("bt-rate").value),
                slippage: Number($("bt-slippage").value),
                size: Number($("bt-size").value),
                pricetick: Number($("bt-pricetick").value),
                capital: Number($("bt-capital").value),
                setting: readParams("bt-params"),
            },
        });
        appendLog(result.message);
    } catch (error) {
        appendLog(error.message);
    }
});

$("data-download-btn").addEventListener("click", async () => {
    try {
        const result = await api("/data/download", {
            method: "POST",
            json: {
                symbol: $("data-symbol").value,
                exchange: $("data-exchange").value,
                interval: $("data-interval").value,
                start: $("data-start").value,
            },
        });
        appendLog(`下载完成 ${result.count} 条`);
        (result.logs || []).forEach(appendLog);
        await refreshData();
    } catch (error) {
        appendLog(error.message);
    }
});

$("data-import-btn").addEventListener("click", async () => {
    const file = $("data-file").files[0];
    if (!file) {
        appendLog("请选择 CSV 文件");
        return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("symbol", $("data-symbol").value);
    form.append("exchange", $("data-exchange").value);
    form.append("interval", $("data-interval").value);
    try {
        const result = await api("/data/import", { method: "POST", body: form });
        appendLog(`导入完成 ${result.count} 条`);
        await refreshData();
    } catch (error) {
        appendLog(error.message);
    }
});

$("data-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-data]");
    if (!button) {
        return;
    }
    const { symbol, exchange, interval, start, end } = button.dataset;
    try {
        if (button.dataset.data === "delete") {
            const result = await api(`/data/bar?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&interval=${encodeURIComponent(interval)}`, { method: "DELETE" });
            appendLog(`已删除 ${result.count} 条`);
            await refreshData();
        } else {
            const response = await fetch(`/data/export?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&interval=${encodeURIComponent(interval)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`, {
                headers: { Authorization: `Bearer ${state.token}` },
            });
            if (!response.ok) {
                throw new Error("导出失败");
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `${symbol}_${exchange}_${interval}.csv`;
            link.click();
            URL.revokeObjectURL(url);
        }
    } catch (error) {
        appendLog(error.message);
    }
});

async function enrollTickUniverse() {
    const portfolios = [];
    if (typeof currentPortfolio === "function") {
        const name = currentPortfolio();
        if (name) {
            portfolios.push(name);
        }
    }
    const result = await api("/recorder/universe", {
        method: "POST",
        json: {
            portfolios,
            max_chains: 0,
            tick: true,
            bar: true,
            init_portfolio: true,
        },
    });
    appendLog(result.message);
    renderRecorder(result);
    if ($("opt-record-status")) {
        $("opt-record-status").textContent = result.message;
    }
    await refreshData();
    return result;
}

if ($("tick-data-body")) {
    $("tick-data-body").addEventListener("click", async (event) => {
        const button = event.target.closest("[data-tick]");
        if (!button) {
            return;
        }
        const { symbol, exchange, start, end } = button.dataset;
        try {
            if (button.dataset.tick === "delete") {
                const result = await api(`/data/tick?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}`, { method: "DELETE" });
                appendLog(`已删除 Tick ${result.count} 条`);
                await refreshData();
            } else {
                const response = await fetch(`/data/tick/export?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`, {
                    headers: { Authorization: `Bearer ${state.token}` },
                });
                if (!response.ok) {
                    throw new Error("Tick 导出失败");
                }
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const link = document.createElement("a");
                link.href = url;
                link.download = `${symbol}_${exchange}_tick.csv`;
                link.click();
                URL.revokeObjectURL(url);
            }
        } catch (error) {
            appendLog(error.message);
        }
    });
}

if ($("rec-universe-btn")) {
    $("rec-universe-btn").addEventListener("click", async () => {
        try {
            await enrollTickUniverse();
        } catch (error) {
            appendLog(error.message);
        }
    });
}

$("rec-add-btn").addEventListener("click", async () => {
    try {
        const result = await api("/recorder", {
            method: "POST",
            json: {
                vt_symbol: $("rec-symbol").value,
                tick: $("rec-tick").checked,
                bar: $("rec-bar").checked,
            },
        });
        appendLog(result.message);
        renderRecorder(result);
    } catch (error) {
        appendLog(error.message);
    }
});

$("rec-remove-btn").addEventListener("click", async () => {
    try {
        const result = await api(`/recorder?vt_symbol=${encodeURIComponent($("rec-symbol").value)}&kind=both`, { method: "DELETE" });
        appendLog(result.message);
        renderRecorder(result);
    } catch (error) {
        appendLog(error.message);
    }
});

async function removeRecording(vtSymbol, kind) {
    const result = await api(`/recorder?vt_symbol=${encodeURIComponent(vtSymbol)}&kind=${encodeURIComponent(kind)}`, { method: "DELETE" });
    appendLog(result.message);
    renderRecorder(result);
}

$("rec-tick-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-rec]");
    if (!button) {
        return;
    }
    try {
        await removeRecording(button.dataset.symbol, button.dataset.kind);
    } catch (error) {
        appendLog(error.message);
    }
});

$("rec-bar-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-rec]");
    if (!button) {
        return;
    }
    try {
        await removeRecording(button.dataset.symbol, button.dataset.kind);
    } catch (error) {
        appendLog(error.message);
    }
});

const today = new Date();
const lastYear = new Date();
lastYear.setFullYear(today.getFullYear() - 1);
$("bt-end").value = today.toISOString().slice(0, 10);
$("bt-start").value = lastYear.toISOString().slice(0, 10);
$("data-start").value = lastYear.toISOString().slice(0, 10);

function parseMap(text) {
    const map = {};
    text.split("\n").forEach((line) => {
        const item = line.trim();
        if (!item || !item.includes("=")) {
            return;
        }
        const [chain, underlying] = item.split("=");
        map[chain.trim()] = underlying.trim();
    });
    return map;
}

function formatMap(map) {
    return Object.entries(map || {}).map(([key, value]) => `${key}=${value}`).join("\n");
}

function currentPortfolio() {
    return $("opt-portfolio").value;
}

let optionRefreshTimer = null;
function scheduleOptionRefresh() {
    if (optionRefreshTimer) {
        clearTimeout(optionRefreshTimer);
    }
    optionRefreshTimer = setTimeout(() => {
        optionRefreshTimer = null;
        refreshOption();
    }, 800);
}

let chainRefreshTimer = null;
function scheduleChainRefresh() {
    if (!$("tab-option") || !$("tab-option").classList.contains("active")) {
        return;
    }
    if (chainRefreshTimer) {
        return;
    }
    chainRefreshTimer = setTimeout(() => {
        chainRefreshTimer = null;
        refreshOptionChain().catch((error) => appendLog(error.message));
    }, 1000);
}

async function refreshOption() {
    const data = await api("/option/portfolio");
    const names = data.portfolios.map((item) => item.name);
    const selected = currentPortfolio();
    fillSelect($("opt-portfolio"), names, selected);
    if (data.models && data.models.length) {
        fillSelect($("opt-model"), data.models, $("opt-model").value);
    }
    const current = data.portfolios.find((item) => item.name === currentPortfolio()) || data.portfolios[0];
    if (current) {
        $("opt-greeks").innerHTML = [
            metricHtml("组合", current.name || ""),
            metricHtml("状态", current.active ? "已激活" : "未激活"),
            metricHtml("净仓", current.net_pos, Number(current.net_pos || 0)),
            metricHtml("Δ", Number(current.pos_delta || 0).toFixed(2), Number(current.pos_delta || 0)),
            metricHtml("Γ", Number(current.pos_gamma || 0).toFixed(4), Number(current.pos_gamma || 0)),
            metricHtml("Vega", Number(current.pos_vega || 0).toFixed(2), Number(current.pos_vega || 0)),
            metricHtml("Theta", Number(current.pos_theta || 0).toFixed(2), Number(current.pos_theta || 0)),
        ].join("");
        if (current.setting) {
            if (current.setting.model_name) {
                $("opt-model").value = current.setting.model_name;
            }
            if (current.setting.interest_rate != null) {
                $("opt-rate").value = current.setting.interest_rate;
            }
            if (current.setting.precision != null) {
                $("opt-precision").value = current.setting.precision;
            }
            $("opt-map").value = formatMap(current.setting.chain_underlying_map);
        }
        fillSelect($("opt-chain"), current.chains || [], $("opt-chain").value);
        if ((!current.setting || !Object.keys(current.setting.chain_underlying_map || {}).length) && current.chains && current.chains.length) {
            $("opt-map").value = current.chains.map((item) => `${item}=${item}`).join("\n");
        }
        await refreshOptionChain();
    }
    const hedge = await api("/option/hedge");
    $("opt-hedge-status").textContent = hedge.active ? `对冲运行中 ${hedge.vt_symbol}` : "对冲未启动";
}

let lastOptionChain = null;
const TV_YIELD_COLORS = ["#3d8bfd", "#f0b429", "#2ecc71", "#ff6b6b", "#54a0ff", "#c084fc", "#22d3ee", "#fb923c"];

function formatGex(value) {
    if (value == null || Number.isNaN(Number(value))) {
        return "—";
    }
    const number = Number(value);
    const abs = Math.abs(number);
    const sign = number < 0 ? "-" : "";
    if (abs >= 1e8) {
        return `${sign}${(abs / 1e8).toFixed(2)}亿`;
    }
    if (abs >= 1e4) {
        return `${sign}${(abs / 1e4).toFixed(2)}万`;
    }
    if (abs >= 100) {
        return number.toFixed(0);
    }
    return number.toFixed(2);
}

function metricHtml(label, value, signed = false, explainKey = "") {
    let cls = "metric";
    const tone = typeof signed === "number" ? signed : (signed === true ? Number(value) : null);
    if (tone != null && !Number.isNaN(tone)) {
        if (tone > 0) {
            cls += " pos";
        } else if (tone < 0) {
            cls += " neg";
        }
    }
    if (explainKey) {
        cls += " clickable";
    }
    const text = value == null || value === "" ? "—" : value;
    const attrs = explainKey ? ` data-explain="${explainKey}" tabindex="0" role="button"` : "";
    return `<div class="${cls}"${attrs}><div class="label">${label}</div><div class="value">${text}</div></div>`;
}

function gexMode() {
    return $("opt-gex-mode") ? $("opt-gex-mode").value : "market";
}

function optionRefSpot(data) {
    const gex = (data && data.gex) || {};
    const stack = gex.stack || {};
    const tv = (data && data.tv_yield) || {};
    const iv = (data && data.iv_smile) || {};
    const value = Number(gex.spot || tv.spot || iv.spot || stack.spot || 0);
    return Number.isFinite(value) && value > 0 ? value : 0;
}

function renderGexSummary(gex) {
    const box = $("opt-gex-summary");
    if (!box) {
        return;
    }
    if (!gex || !gex.strikes || !gex.strikes.length) {
        box.innerHTML = `<p class="hint">初始化组合并收到行情后计算</p>`;
        if ($("opt-gex-legend")) {
            $("opt-gex-legend").textContent = "";
        }
        return;
    }
    const market = gexMode() === "market";
    const stack = gex.stack || {};
    const useStack = (stack.months || []).length > 0;
    const net = market ? gex.net_gex : gex.pos_gex;
    const callGex = market ? gex.call_gex : gex.call_pos_gex;
    const putGex = market ? gex.put_gex : gex.put_pos_gex;
    const flip = useStack
        ? (market ? stack.flip_strike : stack.pos_flip_strike)
        : (market ? gex.flip_strike : gex.pos_flip_strike);
    const metrics = [
        metricHtml("标的", gex.spot ? `${gex.underlying || ""} ${gex.spot}` : "—"),
        metricHtml("净 GEX", formatGex(net), net),
        metricHtml("Call GEX", formatGex(callGex), callGex),
        metricHtml("Put GEX", formatGex(putGex), putGex),
    ];
    if (!market) {
        metrics.push(metricHtml("净持仓", `${Number(gex.pos_lots || 0)} 手`));
    }
    metrics.push(
        metricHtml("Flip", flip ?? "—"),
        metricHtml("Call Wall", (useStack ? (market ? stack.call_wall : stack.pos_call_wall) : (market ? gex.call_wall : gex.pos_call_wall)) ?? "—"),
        metricHtml("Put Wall", (useStack ? (market ? stack.put_wall : stack.pos_put_wall) : (market ? gex.put_wall : gex.pos_put_wall)) ?? "—"),
        metricHtml("Pin", (useStack ? stack.pin : gex.pin) ?? "—"),
        metricHtml("到期", gex.days_to_expiry ? `${gex.days_to_expiry} 天` : "—"),
    );
    box.innerHTML = metrics.join("");
    const bias = Number(net) >= 0 ? "正 GEX：价格易向 Flip 附近回归" : "负 GEX：行情更易加速离开";
    if ($("opt-gex-legend")) {
        const stack = gex.stack || {};
        const months = stack.months || [];
        const useStack = months.length > 0;
        const wallCall = useStack ? (market ? stack.call_wall : stack.pos_call_wall) : (market ? gex.call_wall : gex.pos_call_wall);
        const wallPut = useStack ? (market ? stack.put_wall : stack.pos_put_wall) : (market ? gex.put_wall : gex.pos_put_wall);
        const wallFlip = useStack ? (market ? stack.flip_strike : stack.pos_flip_strike) : flip;
        const wallPin = useStack ? stack.pin : gex.pin;
        const monthKeys = months.map((item, index) => {
            const color = TV_YIELD_COLORS[index % TV_YIELD_COLORS.length];
            return `<span class="gex-key" style="color:${color}">${item.label} ${item.days_to_expiry}天</span>`;
        });
        $("opt-gex-legend").innerHTML = [
            ...monthKeys,
            `<span class="gex-key spot">现价 ${gex.spot ?? stack.spot ?? "—"}</span>`,
            `<span class="gex-key call">Call Wall ${wallCall ?? "—"}</span>`,
            `<span class="gex-key put">Put Wall ${wallPut ?? "—"}</span>`,
            `<span class="gex-key pin">Pin ${wallPin ?? "—"}</span>`,
            `<span class="gex-key flip">Flip ${wallFlip ?? "—"}</span>`,
            `ATM ${gex.atm_price || "—"} ｜ ${market ? bias : (Number(gex.pos_lots || 0) || (useStack && stack.has_pos) ? bias : "账户净持仓为 0")}`,
        ].join(" ｜ ");
    }
}

function strikeToX(strike, rows, padLeft, innerW) {
    const first = Number(rows[0].strike);
    const last = Number(rows[rows.length - 1].strike);
    const value = Number(strike);
    if (!Number.isFinite(value) || last === first) {
        return padLeft + innerW / 2;
    }
    const ratio = (value - first) / (last - first);
    return padLeft + Math.min(1, Math.max(0, ratio)) * innerW;
}

function drawGexLevels(ctx, levels, pad, innerH, cssWidth) {
    const top = pad.top;
    const bottom = pad.top + innerH;
    const placed = [];
    levels.forEach((level) => {
        if (level.strike == null || !Number.isFinite(Number(level.strike))) {
            return;
        }
        const x = level.x;
        ctx.save();
        ctx.strokeStyle = level.color;
        ctx.lineWidth = level.width || 1.25;
        ctx.setLineDash(level.dash || []);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, bottom);
        ctx.stroke();
        ctx.restore();

        let textY = top + 11;
        while (placed.some((item) => Math.abs(item.x - x) < 56 && Math.abs(item.y - textY) < 12)) {
            textY += 12;
        }
        placed.push({ x, y: textY });
        ctx.fillStyle = level.color;
        ctx.font = "11px Microsoft YaHei, sans-serif";
        ctx.textAlign = x > cssWidth - 92 ? "right" : "left";
        ctx.fillText(level.label, x > cssWidth - 92 ? x - 4 : x + 4, textY);
        ctx.textAlign = "left";
    });
}

function renderGexChart(gex) {
    const canvas = $("opt-gex-chart");
    if (!canvas) {
        return;
    }
    const ctx = canvas.getContext("2d");
    const parent = canvas.parentElement;
    const cssWidth = Math.max(320, (parent ? parent.clientWidth : 640) - 32);
    const cssHeight = 280;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = cssWidth * dpr;
    canvas.height = cssHeight * dpr;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const stack = (gex && gex.stack) || {};
    const months = stack.months || [];
    const strikes = stack.strikes || [];
    if (months.length && strikes.length) {
        drawStackedGexChart(ctx, cssWidth, cssHeight, gex, stack);
        return;
    }

    const rows = (gex && gex.strikes) || [];
    if (!rows.length) {
        ctx.fillStyle = "#8b98a6";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.fillText("暂无 GEX 数据", 16, cssHeight / 2);
        return;
    }

    const values = rows.map((item) => (gexMode() === "pos" ? item.pos_gex : item.net_gex) || 0);
    const posMode = gexMode() === "pos";
    const hasPos = posMode && rows.some((item) => Number(item.call_pos || 0) || Number(item.put_pos || 0));
    const maxAbs = Math.max(1, ...values.map((value) => Math.abs(value)));
    const pad = { left: 8, right: 8, top: 40, bottom: 24 };
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    const zeroY = pad.top + innerH / 2;
    const step = innerW / rows.length;
    const barW = Math.max(2, step * 0.7);
    const market = gexMode() === "market";
    const flip = market ? gex.flip_strike : gex.pos_flip_strike;
    const callWall = market ? gex.call_wall : (gex.pos_call_wall ?? gex.call_wall);
    const putWall = market ? gex.put_wall : (gex.pos_put_wall ?? gex.put_wall);

    ctx.strokeStyle = "#2b3642";
    ctx.beginPath();
    ctx.moveTo(pad.left, zeroY);
    ctx.lineTo(cssWidth - pad.right, zeroY);
    ctx.stroke();

    rows.forEach((row, index) => {
        const value = values[index];
        const height = (value / maxAbs) * (innerH / 2 - 4);
        const x = pad.left + index * step + (step - barW) / 2;
        ctx.fillStyle = value >= 0 ? "#2ecc71" : "#ff6b6b";
        if (value >= 0) {
            ctx.fillRect(x, zeroY - height, barW, height);
        } else {
            ctx.fillRect(x, zeroY, barW, -height);
        }
        if (gex.atm_index && row.index === gex.atm_index) {
            ctx.strokeStyle = "#3d8bfd";
            ctx.strokeRect(x - 1, pad.top, barW + 2, innerH);
        }
    });

    const toX = (strike) => strikeToX(strike, rows, pad.left, innerW);
    drawGexLevels(ctx, [
        { strike: gex.spot, x: toX(gex.spot), color: "#e8edf2", label: `现价 ${gex.spot}`, dash: [], width: 1.6 },
        { strike: callWall, x: toX(callWall), color: "#ff9f43", label: `Call Wall ${callWall}`, dash: [6, 3], width: 1.25 },
        { strike: putWall, x: toX(putWall), color: "#54a0ff", label: `Put Wall ${putWall}`, dash: [6, 3], width: 1.25 },
        { strike: gex.pin, x: toX(gex.pin), color: "#b8c0c8", label: `Pin ${gex.pin}`, dash: [2, 3], width: 1.25 },
        { strike: flip, x: toX(flip), color: "#f0b429", label: `Flip ${flip}`, dash: [4, 4], width: 1.1 },
    ], pad, innerH, cssWidth);

    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    ctx.fillText("净GEX", 8, 14);
    ctx.fillText(String(rows[0].strike), pad.left, cssHeight - 8);
    ctx.textAlign = "right";
    ctx.fillText(String(rows[rows.length - 1].strike), cssWidth - pad.right, cssHeight - 8);
    ctx.textAlign = "left";
    if (posMode && !hasPos) {
        ctx.fillStyle = "rgba(15, 22, 30, 0.55)";
        ctx.fillRect(pad.left, pad.top, innerW, innerH);
        ctx.fillStyle = "#c5d0da";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("当前到期月账户净持仓为 0", pad.left + innerW / 2, pad.top + innerH / 2);
        ctx.textAlign = "left";
    }
}

function drawStackedGexChart(ctx, cssWidth, cssHeight, gex, stack) {
    const months = stack.months || [];
    const strikes = stack.strikes || [];
    const key = gexMode() === "pos" ? "pos_gex" : "net_gex";
    const posMode = gexMode() === "pos";
    const pad = { left: 8, right: 8, top: 36, bottom: 24 };
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    const zeroY = pad.top + innerH / 2;
    const step = innerW / strikes.length;
    const barW = Math.max(2, step * 0.72);
    const selected = (lastOptionChain && lastOptionChain.chain_symbol) || "";
    let maxAbs = 1;
    strikes.forEach((_, index) => {
        let up = 0;
        let down = 0;
        months.forEach((month) => {
            const value = Number((month[key] || [])[index] || 0);
            if (value >= 0) {
                up += value;
            } else {
                down -= value;
            }
        });
        maxAbs = Math.max(maxAbs, up, down);
    });

    ctx.strokeStyle = "#2b3642";
    ctx.beginPath();
    ctx.moveTo(pad.left, zeroY);
    ctx.lineTo(cssWidth - pad.right, zeroY);
    ctx.stroke();

    const atm = Number((gex && (gex.spot || gex.atm_price)) || stack.spot);
    let atmIndex = -1;
    if (Number.isFinite(atm) && atm > 0) {
        let best = Infinity;
        strikes.forEach((strike, index) => {
            const dist = Math.abs(Number(strike) - atm);
            if (dist < best) {
                best = dist;
                atmIndex = index;
            }
        });
    }

    strikes.forEach((strike, index) => {
        const x = pad.left + index * step + (step - barW) / 2;
        let posY = zeroY;
        let negY = zeroY;
        months.forEach((month, monthIndex) => {
            const value = Number((month[key] || [])[index] || 0);
            if (!value) {
                return;
            }
            const height = Math.abs(value) / maxAbs * (innerH / 2 - 4);
            const color = TV_YIELD_COLORS[monthIndex % TV_YIELD_COLORS.length];
            ctx.globalAlpha = month.chain_symbol === selected ? 1 : 0.78;
            ctx.fillStyle = color;
            if (value >= 0) {
                posY -= height;
                ctx.fillRect(x, posY, barW, height);
            } else {
                ctx.fillRect(x, negY, barW, height);
                negY += height;
            }
            ctx.globalAlpha = 1;
        });
        if (index === atmIndex) {
            ctx.strokeStyle = "#3d8bfd";
            ctx.strokeRect(x - 1, pad.top, barW + 2, innerH);
        }
    });

    const spot = Number((gex && gex.spot) || stack.spot);
    const rows = strikes.map((strike) => ({ strike }));
    const toX = (level) => strikeToX(level, rows, pad.left, innerW);
    const market = !posMode;
    const flip = market ? stack.flip_strike : stack.pos_flip_strike;
    const callWall = market ? stack.call_wall : (stack.pos_call_wall ?? stack.call_wall);
    const putWall = market ? stack.put_wall : (stack.pos_put_wall ?? stack.put_wall);
    drawGexLevels(ctx, [
        { strike: spot, x: toX(spot), color: "#e8edf2", label: `现价 ${spot || ""}`, dash: [], width: 1.6 },
        { strike: callWall, x: toX(callWall), color: "#ff9f43", label: `Call Wall ${callWall}`, dash: [6, 3], width: 1.25 },
        { strike: putWall, x: toX(putWall), color: "#54a0ff", label: `Put Wall ${putWall}`, dash: [6, 3], width: 1.25 },
        { strike: stack.pin, x: toX(stack.pin), color: "#b8c0c8", label: `Pin ${stack.pin}`, dash: [2, 3], width: 1.25 },
        { strike: flip, x: toX(flip), color: "#f0b429", label: `Flip ${flip}`, dash: [4, 4], width: 1.1 },
    ], pad, innerH, cssWidth);

    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    ctx.fillText("各月净GEX堆积", 8, 14);
    ctx.fillText(String(strikes[0]), pad.left, cssHeight - 8);
    ctx.textAlign = "right";
    ctx.fillText(String(strikes[strikes.length - 1]), cssWidth - pad.right, cssHeight - 8);
    ctx.textAlign = "left";

    let legendX = 92;
    months.forEach((month, index) => {
        const color = TV_YIELD_COLORS[index % TV_YIELD_COLORS.length];
        ctx.fillStyle = color;
        ctx.fillRect(legendX, 6, 10, 10);
        ctx.fillStyle = month.chain_symbol === selected ? "#e8edf2" : "#8b98a6";
        const text = `${month.label} ${month.days_to_expiry}天`;
        ctx.fillText(text, legendX + 14, 15);
        legendX += ctx.measureText(text).width + 28;
    });

    if (posMode && !stack.has_pos) {
        ctx.fillStyle = "rgba(15, 22, 30, 0.55)";
        ctx.fillRect(pad.left, pad.top, innerW, innerH);
        ctx.fillStyle = "#c5d0da";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("账户净持仓为 0", pad.left + innerW / 2, pad.top + innerH / 2);
        ctx.textAlign = "left";
    }
}

function tvYieldSidePoints(item, side) {
    const source = side === "Put"
        ? (item.puts || item.points || [])
        : (item.calls || item.points || []);
    return source
        .filter((point) => !point.option_type || point.option_type === side)
        .slice()
        .sort((a, b) => Number(a.strike) - Number(b.strike));
}

function drawTvYieldSide(ctx, points, xOf, yOf, color, isPut, selected, valueKey = "yield") {
    if (!points.length) {
        return;
    }
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = selected ? 2.4 : 1.5;
    ctx.setLineDash(isPut ? [6, 4] : []);
    ctx.beginPath();
    points.forEach((point, index) => {
        const x = xOf(point.strike);
        const y = yOf(point[valueKey]);
        if (index === 0) {
            ctx.moveTo(x, y);
        } else {
            ctx.lineTo(x, y);
        }
    });
    ctx.stroke();
    ctx.setLineDash([]);
    points.forEach((point) => {
        const x = xOf(point.strike);
        const y = yOf(point[valueKey]);
        if (isPut) {
            ctx.fillRect(x - 3.2, y - 3.2, 6.4, 6.4);
        } else {
            ctx.beginPath();
            ctx.arc(x, y, 3.4, 0, Math.PI * 2);
            ctx.fill();
        }
    });
    ctx.restore();
}

function drawTvYieldStyleLegend(ctx, cssWidth, pad) {
    const items = [
        { label: "Call 实线", put: false },
        { label: "Put 虚线", put: true },
    ];
    let x = cssWidth - pad.right - 168;
    const y = pad.top + 10;
    ctx.save();
    ctx.font = "11px Microsoft YaHei, sans-serif";
    items.forEach((item) => {
        ctx.strokeStyle = "#c5d0da";
        ctx.fillStyle = "#c5d0da";
        ctx.lineWidth = 1.6;
        ctx.setLineDash(item.put ? [5, 4] : []);
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x + 22, y);
        ctx.stroke();
        ctx.setLineDash([]);
        if (item.put) {
            ctx.fillRect(x + 8, y - 3.2, 6.4, 6.4);
        } else {
            ctx.beginPath();
            ctx.arc(x + 11, y, 3.4, 0, Math.PI * 2);
            ctx.fill();
        }
        ctx.fillStyle = "#c5d0da";
        ctx.fillText(item.label, x + 28, y + 4);
        x += 86;
    });
    ctx.restore();
}

function renderTvYieldChart(data) {
    const canvas = $("opt-tv-chart");
    if (!canvas) {
        return;
    }
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 280);
    const payload = (data && data.tv_yield) || {};
    const series = payload.series || [];
    const legend = $("opt-tv-legend");
    if (!series.length) {
        ctx.fillStyle = "#8b98a6";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.fillText("初始化组合并收到行情后计算", 16, cssHeight / 2);
        if (legend) {
            legend.innerHTML = "";
        }
        return;
    }

    const sides = series.flatMap((item) => [
        ...tvYieldSidePoints(item, "Call"),
        ...tvYieldSidePoints(item, "Put"),
    ]);
    const strikes = sides.map((item) => Number(item.strike));
    const yields = sides.map((item) => Number(item.yield));
    const minX = Math.min(...strikes);
    const maxX = Math.max(...strikes);
    const maxY = Math.max(5, ...yields);
    const pad = { left: 52, right: 16, top: 28, bottom: 28 };
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    const xOf = (strike) => {
        if (maxX === minX) {
            return pad.left + innerW / 2;
        }
        return pad.left + (Number(strike) - minX) / (maxX - minX) * innerW;
    };
    const yOf = (value) => pad.top + (maxY - Number(value)) / maxY * innerH;

    ctx.strokeStyle = "#2b3642";
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, pad.top + innerH);
    ctx.lineTo(pad.left + innerW, pad.top + innerH);
    ctx.stroke();

    const spot = optionRefSpot(data) || Number(payload.spot || 0);
    if (spot && spot >= minX && spot <= maxX) {
        const sx = xOf(spot);
        ctx.strokeStyle = "#e8edf2";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(sx, pad.top);
        ctx.lineTo(sx, pad.top + innerH);
        ctx.stroke();
        ctx.lineWidth = 1;
        ctx.fillStyle = "#e8edf2";
        ctx.font = "11px Microsoft YaHei, sans-serif";
        ctx.fillText(`现价 ${spot}`, Math.min(sx + 4, cssWidth - 90), pad.top + innerH - 6);
    }

    const selected = (data && data.chain_symbol) || "";
    series.forEach((item, index) => {
        const color = TV_YIELD_COLORS[index % TV_YIELD_COLORS.length];
        const selectedChain = item.chain_symbol === selected;
        drawTvYieldSide(ctx, tvYieldSidePoints(item, "Call"), xOf, yOf, color, false, selectedChain);
        drawTvYieldSide(ctx, tvYieldSidePoints(item, "Put"), xOf, yOf, color, true, selectedChain);
    });

    drawTvYieldStyleLegend(ctx, cssWidth, pad);

    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    ctx.fillText(`${maxY.toFixed(0)}%`, 4, pad.top + 8);
    ctx.fillText("0%", 4, pad.top + innerH);
    ctx.fillText(String(minX), pad.left, cssHeight - 8);
    ctx.textAlign = "right";
    ctx.fillText(String(maxX), cssWidth - pad.right, cssHeight - 8);
    ctx.textAlign = "left";

    if (legend) {
        const items = series.map((item, index) => {
            const color = TV_YIELD_COLORS[index % TV_YIELD_COLORS.length];
            return `<span class="gex-key" style="color:${color}">${item.label} ${item.days_to_expiry}天</span>`;
        });
        if (spot) {
            items.unshift(`<span class="gex-key spot">现价 ${spot}</span>`);
        }
        items.push('<span class="tv-key call">Call 圆点实线</span>');
        items.push('<span class="tv-key put">Put 方点虚线</span>');
        legend.innerHTML = items.join(" ｜ ");
    }
}

function fillExpiryLegend(legend, series, spot) {
    if (!legend) {
        return;
    }
    const items = series.map((item, index) => {
        const color = TV_YIELD_COLORS[index % TV_YIELD_COLORS.length];
        return `<span class="gex-key" style="color:${color}">${item.label} ${item.days_to_expiry}天</span>`;
    });
    if (spot) {
        items.unshift(`<span class="gex-key spot">现价 ${spot}</span>`);
    }
    items.push('<span class="tv-key call">Call 圆点实线</span>');
    items.push('<span class="tv-key put">Put 方点虚线</span>');
    legend.innerHTML = items.join(" ｜ ");
}

function renderIvSmileChart(data) {
    const canvas = $("opt-iv-chart");
    if (!canvas) {
        return;
    }
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 280);
    const payload = (data && data.iv_smile) || {};
    const series = payload.series || [];
    const legend = $("opt-iv-legend");
    if (!series.length) {
        ctx.fillStyle = "#8b98a6";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.fillText("初始化组合并收到行情后计算", 16, cssHeight / 2);
        if (legend) {
            legend.innerHTML = "";
        }
        return;
    }

    const sides = series.flatMap((item) => [
        ...tvYieldSidePoints(item, "Call"),
        ...tvYieldSidePoints(item, "Put"),
    ]);
    const strikes = sides.map((item) => Number(item.strike));
    const values = sides.map((item) => Number(item.iv)).filter(Number.isFinite);
    const minX = Math.min(...strikes);
    const maxX = Math.max(...strikes);
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    const padY = Math.max(1, (rawMax - rawMin) * 0.12 || 1);
    const minY = Math.max(0, rawMin - padY);
    const maxY = rawMax === rawMin ? rawMax + 5 : rawMax + padY;
    const pad = { left: 52, right: 16, top: 28, bottom: 28 };
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    const xOf = (strike) => {
        if (maxX === minX) {
            return pad.left + innerW / 2;
        }
        return pad.left + (Number(strike) - minX) / (maxX - minX) * innerW;
    };
    const yOf = (value) => pad.top + (maxY - Number(value)) / (maxY - minY) * innerH;

    ctx.strokeStyle = "#2b3642";
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, pad.top + innerH);
    ctx.lineTo(pad.left + innerW, pad.top + innerH);
    ctx.stroke();

    const spot = optionRefSpot(data) || Number(payload.spot || 0);
    if (spot && spot >= minX && spot <= maxX) {
        const sx = xOf(spot);
        ctx.strokeStyle = "#e8edf2";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(sx, pad.top);
        ctx.lineTo(sx, pad.top + innerH);
        ctx.stroke();
        ctx.lineWidth = 1;
        ctx.fillStyle = "#e8edf2";
        ctx.font = "11px Microsoft YaHei, sans-serif";
        ctx.fillText(`现价 ${spot}`, Math.min(sx + 4, cssWidth - 90), pad.top + innerH - 6);
    }

    const selected = (data && data.chain_symbol) || "";
    series.forEach((item, index) => {
        const color = TV_YIELD_COLORS[index % TV_YIELD_COLORS.length];
        const selectedChain = item.chain_symbol === selected;
        drawTvYieldSide(ctx, tvYieldSidePoints(item, "Call"), xOf, yOf, color, false, selectedChain, "iv");
        drawTvYieldSide(ctx, tvYieldSidePoints(item, "Put"), xOf, yOf, color, true, selectedChain, "iv");
    });

    drawTvYieldStyleLegend(ctx, cssWidth, pad);

    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    ctx.fillText(`${maxY.toFixed(1)}%`, 4, pad.top + 8);
    ctx.fillText(`${minY.toFixed(1)}%`, 4, pad.top + innerH);
    ctx.fillText(String(minX), pad.left, cssHeight - 8);
    ctx.textAlign = "right";
    ctx.fillText(String(maxX), cssWidth - pad.right, cssHeight - 8);
    ctx.textAlign = "left";
    fillExpiryLegend(legend, series, spot);
}

function nearestFlipIndex(gex) {
    const flip = gexMode() === "pos" ? gex && gex.pos_flip_strike : gex && gex.flip_strike;
    const rows = (gex && gex.strikes) || [];
    if (flip == null || !rows.length) {
        return null;
    }
    return rows.reduce((best, item) => (
        Math.abs(item.strike - flip) < Math.abs(best.strike - flip) ? item : best
    )).index;
}

function chainRowClass(row, gex) {
    const classes = [];
    const item = ((gex && gex.strikes) || []).find((strike) => strike.index === row.index);
    if (gex && gex.atm_index && row.index === gex.atm_index) {
        classes.push("atm");
    }
    if (item && gex && (item.strike === gex.call_wall || item.strike === gex.put_wall
        || item.strike === gex.pos_call_wall || item.strike === gex.pos_put_wall)) {
        classes.push("wall");
    }
    if (item && gex && item.strike === gex.pin) {
        classes.push("pin");
    }
    if (row.index === nearestFlipIndex(gex)) {
        classes.push("flip");
    }
    return classes.join(" ");
}

function gexByIndex(gex, index) {
    return ((gex && gex.strikes) || []).find((item) => item.index === index) || {};
}

async function refreshOptionChain() {
    const name = currentPortfolio();
    if (!name) {
        return;
    }
    const chain = $("opt-chain").value;
    const data = await api(`/option/chain?portfolio_name=${encodeURIComponent(name)}&chain_symbol=${encodeURIComponent(chain)}`);
    lastOptionChain = data;
    fillSelect($("opt-chain"), data.chains || [], data.chain_symbol);
    renderOptionChainView(data);
}

let optionPollTimer = null;
function startOptionPoll() {
    if (optionPollTimer) {
        return;
    }
    optionPollTimer = setInterval(() => {
        if (!$("tab-option") || !$("tab-option").classList.contains("active")) {
            return;
        }
        refreshOptionChain().catch(() => {});
    }, 1500);
}

function renderOptionChainView(data) {
    const gex = (data && data.gex) || {};
    renderGexSummary(gex);
    renderGexChart(gex);
    renderTvYieldChart(data);
    renderIvSmileChart(data);
    renderTable("opt-chain-body", data.rows || [], (row) => {
        const call = row.call || {};
        const put = row.put || {};
        const item = gexByIndex(gex, row.index);
        const callGex = gexMode() === "pos" ? (item.call_pos_gex || 0) : (item.call_gex || 0);
        const putGex = gexMode() === "pos" ? (item.put_pos_gex || 0) : (item.put_gex || 0);
        return `<tr class="${chainRowClass(row, gex)}">
            <td>${call.vt_symbol || ""}</td>
            <td>${call.bid_price ?? ""}/${call.ask_price ?? ""}</td>
            <td>${call.mid_impv ?? ""}</td>
            <td>${Number(call.theo_delta || 0).toFixed(3)}</td>
            <td>${call.open_interest ?? item.call_oi ?? ""}</td>
            <td class="${Number(callGex) >= 0 ? "buy" : "sell"}">${formatGex(callGex)}</td>
            <td>${row.index || ""}</td>
            <td class="${Number(putGex) >= 0 ? "buy" : "sell"}">${formatGex(putGex)}</td>
            <td>${put.open_interest ?? item.put_oi ?? ""}</td>
            <td>${Number(put.theo_delta || 0).toFixed(3)}</td>
            <td>${put.mid_impv ?? ""}</td>
            <td>${put.bid_price ?? ""}/${put.ask_price ?? ""}</td>
            <td>${put.vt_symbol || ""}</td>
        </tr>`;
    });
}

let futuresProductTimer = null;
let futuresCurveTimer = null;

function scheduleFuturesProductsRefresh() {
    if (futuresProductTimer) {
        return;
    }
    futuresProductTimer = setTimeout(() => {
        futuresProductTimer = null;
        refreshFuturesProducts();
    }, 1500);
}

function scheduleFuturesCurveRefresh() {
    if (!$("tab-futures") || !$("tab-futures").classList.contains("active")) {
        return;
    }
    if (futuresCurveTimer) {
        return;
    }
    futuresCurveTimer = setTimeout(() => {
        futuresCurveTimer = null;
        refreshFuturesCurve();
    }, 1000);
}

function currentFuturesKey() {
    return $("fut-product") ? $("fut-product").value : state.futuresProductKey;
}

function filteredFuturesProducts() {
    const keyword = ($("fut-search") && $("fut-search").value || "").trim().toLowerCase();
    const items = state.futuresProducts || [];
    if (!keyword) {
        return items;
    }
    return items.filter((item) => {
        const text = `${item.key} ${item.name} ${item.exchange}`.toLowerCase();
        return text.includes(keyword);
    });
}

function fillFuturesProductSelect() {
    const select = $("fut-product");
    if (!select) {
        return;
    }
    const selected = currentFuturesKey() || state.futuresProductKey;
    const items = filteredFuturesProducts();
    select.innerHTML = "";
    items.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.key;
        option.textContent = `${item.key} ${item.name} (${item.months}月)`;
        if (item.key === selected) {
            option.selected = true;
        }
        select.appendChild(option);
    });
    if (select.value) {
        state.futuresProductKey = select.value;
    }
}

async function refreshFuturesProducts() {
    const products = await api("/futures/products");
    state.futuresProducts = products || [];
    fillFuturesProductSelect();
    if (!state.futuresProducts.length) {
        $("fut-status").textContent = "连接交易接口后，合约查询完成即可选择品种";
        return;
    }
    $("fut-status").textContent = `共 ${state.futuresProducts.length} 个品种`;
    if (currentFuturesKey() && !state.futuresCurve) {
        await subscribeAndRefreshFutures();
    }
}

function formatBasis(value) {
    if (value == null || value === "" || Number(value) === 0) {
        return "0";
    }
    const number = Number(value);
    const sign = number > 0 ? "+" : "";
    return `${sign}${Math.abs(number) >= 100 ? number.toFixed(1) : number.toFixed(2)}`;
}

function formatOi(value) {
    const number = Number(value || 0);
    if (Math.abs(number) >= 1e4) {
        return `${(number / 1e4).toFixed(2)}万`;
    }
    return String(Math.round(number));
}

function signedClass(value) {
    const number = Number(value || 0);
    if (number > 0) {
        return "buy";
    }
    if (number < 0) {
        return "sell";
    }
    return "";
}

function setupCanvas(canvas, height = 220) {
    const ctx = canvas.getContext("2d");
    const parent = canvas.parentElement;
    const cssWidth = Math.max(280, (parent ? parent.clientWidth : 480) - 32);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = cssWidth * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, height);
    return { ctx, cssWidth, cssHeight: height };
}

function renderFuturesCharts(data) {
    const rows = (data && data.rows) || [];
    const priceCanvas = $("fut-price-chart");
    const oiCanvas = $("fut-oi-chart");
    if (priceCanvas) {
        const { ctx, cssWidth, cssHeight } = setupCanvas(priceCanvas);
        const priced = rows.filter((row) => row.price);
        if (!priced.length) {
            ctx.fillStyle = "#8b98a6";
            ctx.font = "12px Microsoft YaHei, sans-serif";
            ctx.fillText("订阅后等待行情", 16, cssHeight / 2);
        } else {
            const pad = { left: 44, right: 12, top: 16, bottom: 28 };
            const innerW = cssWidth - pad.left - pad.right;
            const innerH = cssHeight - pad.top - pad.bottom;
            const prices = priced.map((row) => row.price);
            const minP = Math.min(...prices);
            const maxP = Math.max(...prices);
            const span = Math.max(maxP - minP, Math.abs(maxP) * 0.002, 1e-6);
            const yOf = (price) => pad.top + (maxP - price) / span * innerH;
            const xOf = (index) => pad.left + (priced.length === 1 ? innerW / 2 : index / (priced.length - 1) * innerW);

            ctx.strokeStyle = "#2b3642";
            ctx.beginPath();
            ctx.moveTo(pad.left, pad.top);
            ctx.lineTo(pad.left, pad.top + innerH);
            ctx.lineTo(pad.left + innerW, pad.top + innerH);
            ctx.stroke();

            ctx.strokeStyle = "#3d8bfd";
            ctx.lineWidth = 2;
            ctx.beginPath();
            priced.forEach((row, index) => {
                const x = xOf(index);
                const y = yOf(row.price);
                if (index === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            });
            ctx.stroke();
            ctx.lineWidth = 1;
            priced.forEach((row, index) => {
                const x = xOf(index);
                const y = yOf(row.price);
                ctx.fillStyle = row.is_dominant ? "#3d8bfd" : (row.is_front ? "#f0b429" : "#d7dee7");
                ctx.beginPath();
                ctx.arc(x, y, row.is_dominant ? 5 : 3, 0, Math.PI * 2);
                ctx.fill();
            });
            ctx.fillStyle = "#8b98a6";
            ctx.font = "11px Microsoft YaHei, sans-serif";
            ctx.fillText(String(priced[0].month), pad.left, cssHeight - 8);
            ctx.textAlign = "right";
            ctx.fillText(String(priced[priced.length - 1].month), cssWidth - pad.right, cssHeight - 8);
            ctx.textAlign = "left";
            ctx.fillText(maxP.toFixed(2), 4, pad.top + 8);
            ctx.fillText(minP.toFixed(2), 4, pad.top + innerH);
        }
    }
    if (oiCanvas) {
        const { ctx, cssWidth, cssHeight } = setupCanvas(oiCanvas);
        if (!rows.length) {
            ctx.fillStyle = "#8b98a6";
            ctx.font = "12px Microsoft YaHei, sans-serif";
            ctx.fillText("暂无持仓数据", 16, cssHeight / 2);
        } else {
            const pad = { left: 8, right: 8, top: 16, bottom: 28 };
            const innerW = cssWidth - pad.left - pad.right;
            const innerH = cssHeight - pad.top - pad.bottom;
            const maxOi = Math.max(1, ...rows.map((row) => row.open_interest || 0));
            const step = innerW / rows.length;
            const barW = Math.max(4, step * 0.7);
            rows.forEach((row, index) => {
                const height = (row.open_interest || 0) / maxOi * innerH;
                const x = pad.left + index * step + (step - barW) / 2;
                ctx.fillStyle = row.is_dominant ? "#3d8bfd" : "#5b6b7c";
                ctx.fillRect(x, pad.top + innerH - height, barW, height);
            });
            ctx.fillStyle = "#8b98a6";
            ctx.font = "11px Microsoft YaHei, sans-serif";
            ctx.fillText(rows[0].month, pad.left, cssHeight - 8);
            ctx.textAlign = "right";
            ctx.fillText(rows[rows.length - 1].month, cssWidth - pad.right, cssHeight - 8);
            ctx.textAlign = "left";
        }
    }
}

function capitalMode() {
    return $("fut-capital-mode") ? $("fut-capital-mode").value : "notional";
}

function monthCapitalParts(item) {
    if (capitalMode() === "premium") {
        return {
            futures: Number(item.futures_notional || 0),
            call: Number(item.call_premium || 0),
            put: Number(item.put_premium || 0),
        };
    }
    return {
        futures: Number(item.futures_notional || 0),
        call: Number(item.call_notional || 0),
        put: Number(item.put_notional || 0),
    };
}

function strikeCapitalParts(item) {
    if (capitalMode() === "premium") {
        return { call: Number(item.call_premium || 0), put: Number(item.put_premium || 0) };
    }
    return { call: Number(item.call_notional || 0), put: Number(item.put_notional || 0) };
}

function drawStackedBars(canvas, labels, series, colors, emptyText) {
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 260);
    if (!labels.length) {
        ctx.fillStyle = "#8b98a6";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.fillText(emptyText || "暂无数据", 16, cssHeight / 2);
        return;
    }
    const pad = { left: 58, right: 12, top: 28, bottom: 28 };
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    const totals = labels.map((_, index) => series.reduce((sum, item) => sum + (item.values[index] || 0), 0));
    const maxVal = Math.max(1, ...totals);
    const step = innerW / labels.length;
    const barW = Math.max(6, step * 0.62);

    ctx.strokeStyle = "#2b3642";
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, pad.top + innerH);
    ctx.lineTo(pad.left + innerW, pad.top + innerH);
    ctx.stroke();

    labels.forEach((_, index) => {
        let y = pad.top + innerH;
        const x = pad.left + index * step + (step - barW) / 2;
        series.forEach((item, seriesIndex) => {
            const value = item.values[index] || 0;
            const height = value / maxVal * innerH;
            ctx.fillStyle = colors[seriesIndex];
            ctx.fillRect(x, y - height, barW, height);
            y -= height;
        });
    });

    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    ctx.fillText(String(labels[0]), pad.left, cssHeight - 8);
    ctx.textAlign = "right";
    ctx.fillText(String(labels[labels.length - 1]), cssWidth - pad.right, cssHeight - 8);
    ctx.textAlign = "left";
    ctx.fillText(formatGex(maxVal), 4, pad.top + 8);

    let legendX = pad.left;
    series.forEach((item, index) => {
        ctx.fillStyle = colors[index];
        ctx.fillRect(legendX, 8, 10, 10);
        ctx.fillStyle = "#8b98a6";
        ctx.fillText(item.name, legendX + 14, 17);
        legendX += ctx.measureText(item.name).width + 28;
    });
}

function fillCapitalMonthSelect(months) {
    const select = $("fut-capital-month");
    if (!select) {
        return;
    }
    const previous = select.value;
    select.innerHTML = "";
    months.forEach((item) => {
        const option = document.createElement("option");
        option.value = String(item.yyyymm);
        const chain = (item.call_count || 0) + (item.put_count || 0);
        option.textContent = chain ? `${item.month} 全链${chain}档` : String(item.month);
        select.appendChild(option);
    });
    const values = months.map((item) => String(item.yyyymm));
    if (previous && values.includes(previous)) {
        select.value = previous;
        return;
    }
    const best = [...months].sort((a, b) => (
        (b.option_notional || 0) - (a.option_notional || 0)
        || (b.futures_notional || 0) - (a.futures_notional || 0)
    ))[0];
    if (best) {
        select.value = String(best.yyyymm);
    }
}

function selectedCapitalMonth(data) {
    const months = (data && data.capital && data.capital.months) || [];
    const yyyymm = Number($("fut-capital-month") && $("fut-capital-month").value);
    return months.find((item) => item.yyyymm === yyyymm) || months[0];
}

function renderFuturesCapitalCharts(data) {
    const months = (data && data.capital && data.capital.months) || [];
    const capitalCanvas = $("fut-capital-chart");
    const chainCanvas = $("fut-chain-chart");
    if (capitalCanvas) {
        drawStackedBars(
            capitalCanvas,
            months.map((item) => item.month),
            [
                { name: "期货", values: months.map((item) => monthCapitalParts(item).futures) },
                { name: "Call", values: months.map((item) => monthCapitalParts(item).call) },
                { name: "Put", values: months.map((item) => monthCapitalParts(item).put) },
            ],
            ["#3d8bfd", "#2ecc71", "#ff6b6b"],
            "订阅期权链后显示资金沉淀",
        );
    }
    if (chainCanvas) {
        const month = selectedCapitalMonth(data);
        const strikes = (month && month.strikes) || [];
        drawStackedBars(
            chainCanvas,
            strikes.map((item) => item.strike),
            [
                { name: "Call", values: strikes.map((item) => strikeCapitalParts(item).call) },
                { name: "Put", values: strikes.map((item) => strikeCapitalParts(item).put) },
            ],
            ["#2ecc71", "#ff6b6b"],
            "该月暂无期权链行情",
        );
    }
    const legend = $("fut-capital-legend");
    if (legend) {
        const mode = capitalMode() === "premium" ? "期权为权利金沉淀（持仓×权利金×乘数）" : "期权为名义本金（持仓×标的价×乘数）";
        const count = (data && data.capital && data.capital.option_contracts) || 0;
        legend.textContent = `期货：持仓×价格×乘数。${mode}。关联期权 ${count} 个。`;
    }
}

function renderFuturesCurve(data) {
    state.futuresCurve = data;
    const box = $("fut-summary");
    const rows = (data && data.rows) || [];
    if (!box) {
        return;
    }
    if (!rows.length) {
        box.innerHTML = `<p class="hint">选择品种并订阅后，显示各月持仓与升贴水</p>`;
        fillCapitalMonthSelect([]);
        renderFuturesCharts(data);
        renderFuturesCapitalCharts(data);
        renderTable("fut-body", [], () => "");
        return;
    }
    const totals = (data.capital && data.capital.totals) || {};
    box.innerHTML = [
        metricHtml("品种", `${data.name || ""} ${data.product_key || ""}`),
        metricHtml("主力", data.dominant || "—"),
        metricHtml("近月", data.front || "—"),
        metricHtml("总持仓", formatOi(data.total_oi)),
        metricHtml("近远结构", `${data.structure_label || "—"} ${formatBasis(data.structure)}`, data.structure),
        metricHtml("期货沉淀", formatGex(totals.futures_notional)),
        metricHtml("期权名义", formatGex(totals.option_notional)),
        metricHtml("期权权利金", formatGex(totals.option_premium)),
    ].join("");
    fillCapitalMonthSelect((data.capital && data.capital.months) || []);
    renderFuturesCharts(data);
    renderFuturesCapitalCharts(data);
    const capitalMonths = (data.capital && data.capital.months) || [];
    renderTable("fut-body", rows, (row) => {
        const cls = [row.is_dominant ? "dominant" : "", row.is_front ? "front" : ""].join(" ").trim();
        const vsFrontPct = row.vs_front_pct ? ` ${formatBasis(row.vs_front_pct)}%` : "";
        const cap = capitalMonths.find((item) => item.yyyymm === row.yyyymm) || {};
        const parts = monthCapitalParts(cap);
        return `<tr class="${cls}">
            <td>${row.vt_symbol || ""}</td>
            <td>${row.name || ""}</td>
            <td>${row.price || "—"}</td>
            <td class="${signedClass(row.change)}">${row.price ? formatBasis(row.change) : "—"}</td>
            <td>${formatOi(row.open_interest)}</td>
            <td>${row.oi_ratio ? `${row.oi_ratio}%` : "—"}</td>
            <td>${formatOi(row.volume)}</td>
            <td class="${signedClass(row.vs_dominant)}">${row.price ? formatBasis(row.vs_dominant) : "—"}</td>
            <td class="${signedClass(row.vs_front)}">${row.price ? `${formatBasis(row.vs_front)}${vsFrontPct}` : "—"}</td>
            <td class="${signedClass(row.spread)}">${row.spread ? formatBasis(row.spread) : "—"}</td>
            <td>${formatGex(parts.futures)}</td>
            <td>${formatGex(parts.call)}</td>
            <td>${formatGex(parts.put)}</td>
            <td class="${signedClass(row.account_pos)}">${row.account_pos || ""}</td>
        </tr>`;
    });
}

async function refreshFuturesCurve() {
    const key = currentFuturesKey();
    if (!key) {
        return;
    }
    const data = await api(`/futures/curve?product_key=${encodeURIComponent(key)}`);
    renderFuturesCurve(data);
}

async function subscribeAndRefreshFutures() {
    const key = currentFuturesKey();
    if (!key) {
        appendLog("请先选择期货品种");
        return;
    }
    state.futuresProductKey = key;
    try {
        const includeOptions = $("fut-sub-opt") ? $("fut-sub-opt").checked : true;
        const result = await api(
            `/futures/subscribe?product_key=${encodeURIComponent(key)}&include_options=${includeOptions}`,
            { method: "POST" },
        );
        $("fut-status").textContent = result.message;
        appendLog(result.message);
        await refreshFuturesCurve();
    } catch (error) {
        appendLog(error.message);
        $("fut-status").textContent = error.message;
    }
}

function renderSpreads() {
    renderTable("sp-body", Object.values(state.spreads), (item) => `
        <tr>
            <td>${item.name || ""}</td>
            <td>${item.bid_volume ?? ""}</td>
            <td>${item.bid_price ?? ""}</td>
            <td>${item.ask_price ?? ""}</td>
            <td>${item.ask_volume ?? ""}</td>
            <td>${item.net_pos ?? ""}</td>
            <td><button class="small danger" data-spread-del="${item.name}">删除</button></td>
        </tr>`);
}

function renderSpreadStrategies() {
    renderTable("sp-stg-body", Object.values(state.spreadStrategies), (item) => `
        <tr>
            <td>${item.strategy_name || ""}</td>
            <td>${item.class_name || ""}</td>
            <td>${item.spread_name || ""}</td>
            <td>${item.variables && item.variables.inited ? "是" : "否"}</td>
            <td>${item.variables && item.variables.trading ? "是" : "否"}</td>
            <td>
                <button class="small ok" data-spstg="init" data-name="${item.strategy_name}">初始化</button>
                <button class="small" data-spstg="start" data-name="${item.strategy_name}">启动</button>
                <button class="small ghost" data-spstg="stop" data-name="${item.strategy_name}">停止</button>
                <button class="small danger" data-spstg="remove" data-name="${item.strategy_name}">删除</button>
            </td>
        </tr>`);
}

async function refreshSpread() {
    const spreads = await api("/spread");
    state.spreads = {};
    spreads.forEach((item) => { state.spreads[item.name] = item; });
    renderSpreads();
    const names = spreads.map((item) => item.name);
    fillSelect($("sp-algo-name"), names, $("sp-algo-name").value);
    fillSelect($("sp-stg-spread"), names, $("sp-stg-spread").value);
    const classes = await api("/spread/class");
    fillSelect($("sp-class"), classes, $("sp-class").value);
    if (classes[0] && !$("sp-params").children.length) {
        paramFields("sp-params", await api(`/spread/class/${classes[0]}`));
    }
    const strategies = await api("/spread/strategy");
    state.spreadStrategies = {};
    strategies.forEach((item) => { state.spreadStrategies[item.strategy_name] = item; });
    renderSpreadStrategies();
}

async function refreshScript() {
    try {
        const data = await api("/script");
        $("script-status").textContent = data.active ? "运行中" : "未运行";
        fillScriptFileSelect(data.files || [], $("script-file").value);
    } catch (error) {
        $("script-status").textContent = "未运行";
        fillScriptFileSelect([], $("script-file").value);
        appendLog(error.message);
    }
    try {
        await refreshScriptMonitor();
    } catch (error) {
        // 监控依赖脚本引擎，列表接口失败时仍保留页面兜底下拉
    }
}

async function refreshLive() {
    if (!$("tab-live")) {
        return;
    }
    const [status, monitor] = await Promise.all([
        api("/live/status"),
        api("/live/monitor"),
    ]);
    fillLiveConfig(status.config || monitor.config || {});
    renderLiveStatus(status, monitor);
    renderLiveMonitor(monitor);
}

function fillLiveConfig(config) {
    const setVal = (id, value) => {
        const el = $(id);
        if (!el || value == null) {
            return;
        }
        if (el.type === "checkbox") {
            el.checked = Boolean(value);
        } else {
            el.value = value;
        }
    };
    setVal("live-portfolios", config.portfolios);
    setVal("live-script", config.script);
    setVal("live-gateway", config.gateway);
    setVal("live-dry-run", config.dry_run);
    setVal("live-auto-script", config.auto_start_script !== false);
    setVal("live-wing-steps", config.wing_steps);
    setVal("live-min-credit", config.min_credit_frac);
    setVal("live-iv-rank", config.iv_rank_min);
    setVal("live-risk-cap", config.risk_cap);
    setVal("live-max-lots", config.max_lots);
    setVal("live-roll-dte", config.roll_dte);
    setVal("live-min-delta", config.min_delta);
    setVal("live-max-delta", config.max_delta);
    setVal("live-take-profit", config.take_profit);
}

function readLiveConfig() {
    return {
        portfolios: $("live-portfolios").value.trim() || "IO.CFFEX",
        script: $("live-script").value,
        gateway: $("live-gateway").value.trim() || "CTP",
        dry_run: $("live-dry-run").checked,
        auto_start_script: $("live-auto-script").checked,
        wing_steps: Number($("live-wing-steps").value || 5),
        min_credit_frac: Number($("live-min-credit").value || 0.3),
        iv_rank_min: Number($("live-iv-rank").value || 40),
        risk_cap: Number($("live-risk-cap").value || 0.06),
        max_lots: Number($("live-max-lots").value || 80),
        roll_dte: Number($("live-roll-dte").value || 21),
        min_delta: Number($("live-min-delta").value || 0.14),
        max_delta: Number($("live-max-delta").value || 0.25),
        take_profit: Number($("live-take-profit").value || 0.25),
        enabled: true,
    };
}

function renderLiveStatus(status, monitor) {
    const pills = $("live-status-pills");
    const hint = $("live-control-hint");
    const supervisor = (status && status.supervisor) || (monitor && monitor.supervisor) || {};
    const active = Boolean((status && status.script_active) || (monitor && (monitor.engine_active || monitor.active)));
    const items = [
        { text: supervisor.enabled ? (supervisor.paused ? "守护暂停" : "守护运行") : "守护关闭", cls: supervisor.enabled ? (supervisor.paused ? "warn" : "on") : "off" },
        { text: supervisor.ctp_ok || status.gateway_connected ? "CTP已连" : "CTP未连", cls: supervisor.ctp_ok || status.gateway_connected ? "on" : "off" },
        { text: supervisor.session_open ? "交易时段" : "非交易时段", cls: supervisor.session_open ? "on" : "warn" },
        { text: active ? "策略运行" : "策略停止", cls: active ? "on" : "off" },
        { text: (monitor && monitor.dry_run) ? "DRY RUN" : "实盘", cls: (monitor && monitor.dry_run) ? "warn" : "on" },
    ];
    if (pills) {
        pills.innerHTML = items.map((item) => `<span class="live-pill ${item.cls}">${item.text}</span>`).join("");
    }
    if (hint) {
        const reason = (monitor && monitor.reason) || "";
        hint.textContent = reason
            ? `最近决策：${reason}`
            : `组合 ${(status.config && status.config.portfolios) || "—"} ｜ 更新 ${(monitor && monitor.updated) || (status && status.updated) || "—"}`;
    }
}

function renderLiveMonitor(data) {
    state.liveMonitor = data || null;
    const runBox = $("live-run-metrics");
    const indBox = $("live-indicators");
    const sigBox = $("live-signals");
    const bookBox = $("live-book");
    const logBox = $("live-decision-log");
    const engBox = $("live-engine-log");
    const indicators = data.indicators || {};
    const book = data.book || {};
    const pick = data.pick || {};
    const params = data.params || {};
    if (runBox) {
        runBox.innerHTML = [
            metricHtml("引擎", (data.engine_active || data.active) ? "运行中" : "未运行"),
            metricHtml("模式", data.dry_run ? "DRY RUN" : "实盘", data.dry_run ? -1 : 1),
            metricHtml("组合", data.portfolio || params.portfolio_name || "—"),
            metricHtml("链", data.chain || indicators.chain || "—"),
            metricHtml("标的", indicators.spot ?? data.spot ?? "—", false, "spot"),
            metricHtml("IV Rank", indicators.iv_rank ?? data.iv_rank ?? "—", data.iv_high ? 1 : -1, "iv_rank"),
            metricHtml("手数", book.lots ?? 0, Number(book.lots || 0)),
            metricHtml("净值份额", indicators.nav ?? data.nav ?? "—"),
            metricHtml("状态", data.reason || "正常", data.reason ? 0 : 1),
        ].join("");
    }
    if (indBox) {
        const kelly = indicators.kelly || data.kelly || {};
        indBox.innerHTML = [
            metricHtml("IV", indicators.iv ?? "—", false, "iv"),
            metricHtml("LSP", indicators.lsp ?? "—", false, "lsp"),
            metricHtml("DTE", indicators.dte ?? "—", false, "dte"),
            metricHtml("Call墙", indicators.call_wall ?? "—", false, "call_wall"),
            metricHtml("Put墙", indicators.put_wall ?? "—", false, "put_wall"),
            metricHtml("权利金", indicators.entry_credit ?? book.entry_credit ?? "—", false, "entry_credit"),
            metricHtml("Kelly f", kelly.f != null ? Number(kelly.f).toFixed(3) : "—", false, "kelly"),
            metricHtml("θ/风险", indicators.pick_efficiency ?? pick.efficiency ?? "—", false, "efficiency"),
            metricHtml("存活概率", indicators.pick_range_prob ?? pick.range_prob ?? "—", false, "range_prob"),
        ].join("");
    }
    if (sigBox) {
        const signals = data.signals || [];
        sigBox.innerHTML = signals.length
            ? signals.map((item) => `
                <div class="signal-item ${item.ok ? "ok" : ""}">
                    <span class="dot"></span>
                    <div>
                        <div class="title">${item.label || item.id || ""}</div>
                        <div class="detail">${item.detail || (item.ok ? "通过" : "未满足")}</div>
                    </div>
                </div>`).join("")
            : `<p class="hint">等待策略信号</p>`;
    }
    if (bookBox) {
        bookBox.innerHTML = [
            metricHtml("到期", book.expiry || "—"),
            metricHtml("短Call", book.k_call || "—"),
            metricHtml("短Put", book.k_put || "—"),
            metricHtml("长Call", book.k_call_long || "—"),
            metricHtml("长Put", book.k_put_long || "—"),
            metricHtml("短Call合约", book.call_symbol || "—"),
            metricHtml("短Put合约", book.put_symbol || "—"),
            metricHtml("候选", pick.k_put_long
                ? `${pick.k_put_long}/${pick.k_put}/${pick.k_call}/${pick.k_call_long}`
                : "—"),
            metricHtml("候选权利金", pick.credit ?? "—", false, "entry_credit"),
        ].join("");
    }
    renderTable("live-market-body", data.market || [], (row) => `
        <tr class="${row.missing ? "skip" : ""}">
            <td>${row.vt_symbol || ""}</td>
            <td>${row.last_price ?? "—"}</td>
            <td>${row.bid_price_1 ?? "—"}</td>
            <td>${row.ask_price_1 ?? "—"}</td>
            <td>${row.volume ?? "—"}</td>
            <td>${row.pricetick ?? "—"}</td>
        </tr>`);
    const posRows = [];
    (data.accounts || []).forEach((item) => {
        posRows.push({
            kind: "账户",
            code: item.accountid || "",
            direction: "",
            volume: item.balance ?? "",
            price: item.available ?? "",
            pnl: item.frozen ?? "",
        });
    });
    (data.positions || []).forEach((item) => {
        posRows.push({
            kind: "持仓",
            code: item.vt_symbol || "",
            direction: item.direction || "",
            volume: item.volume ?? "",
            price: item.price ?? "",
            pnl: item.pnl ?? "",
        });
    });
    renderTable("live-pos-body", posRows, (row) => `
        <tr>
            <td>${row.kind}</td>
            <td>${row.code}</td>
            <td class="${sideClass(row.direction)}">${row.direction}</td>
            <td>${row.volume}</td>
            <td>${row.price}</td>
            <td class="${signedClass(row.pnl)}">${row.pnl}</td>
        </tr>`);
    if (logBox) {
        const lines = data.decisions || [];
        logBox.textContent = lines.length ? lines.join("\n") : "等待策略输出";
        logBox.scrollTop = logBox.scrollHeight;
    }
    if (engBox) {
        const logs = data.logs || [];
        engBox.textContent = logs.length
            ? logs.map((item) => `${item.time || ""} ${item.msg || ""}`).join("\n")
            : "—";
        engBox.scrollTop = engBox.scrollHeight;
    }
}

function closeLiveExplainModal() {
    const modal = $("live-explain-modal");
    if (!modal) {
        return;
    }
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
}

function openLiveExplainModal(key) {
    const modal = $("live-explain-modal");
    if (!modal) {
        return;
    }
    const explains = (state.liveMonitor && state.liveMonitor.explains) || {};
    const payload = explains[key];
    if (!payload) {
        appendLog(`暂无 ${key} 的计算说明`);
        return;
    }
    $("live-explain-title").textContent = payload.title || key;
    const value = payload.value == null || payload.value === "" ? "—" : payload.value;
    $("live-explain-value").textContent = `当前值：${value}`;
    $("live-explain-formula").textContent = payload.formula || "";
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    $("live-explain-steps").innerHTML = steps.map((item) => `<li>${item}</li>`).join("");
    const chart = payload.chart || {};
    const hint = $("live-explain-chart-hint");
    if (hint) {
        if (chart.type === "gex_walls" && !(chart.strikes || []).length) {
            hint.textContent = "链上 GEX 剖面暂不可用（需期权组合已初始化并收到行情）";
        } else if (chart.type === "gex_walls") {
            hint.textContent = "柱状为各行权价 CallGEX（橙）/ PutGEX（蓝）；竖线为策略墙与现价";
        } else {
            hint.textContent = "";
        }
    }
    drawLiveExplainChart(chart);
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
}

function drawLiveExplainChart(chart) {
    const canvas = $("live-explain-chart");
    if (!canvas) {
        return;
    }
    const parent = canvas.parentElement;
    const cssWidth = Math.max(320, (parent ? parent.clientWidth : 640) - 8);
    const cssHeight = 280;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = cssWidth * dpr;
    canvas.height = cssHeight * dpr;
    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);
    ctx.fillStyle = "rgba(255,255,255,0.02)";
    ctx.fillRect(0, 0, cssWidth, cssHeight);
    const type = chart && chart.type;
    if (type === "gex_walls" || type === "spot_walls") {
        drawExplainGexWalls(ctx, cssWidth, cssHeight, chart);
        return;
    }
    if (type === "gauge") {
        drawExplainGauge(ctx, cssWidth, cssHeight, chart);
        return;
    }
    if (type === "bar_single") {
        drawExplainBar(ctx, cssWidth, cssHeight, chart);
        return;
    }
    if (type === "legs") {
        drawExplainLegs(ctx, cssWidth, cssHeight, chart);
        return;
    }
    ctx.fillStyle = "#8b98a8";
    ctx.font = "13px Microsoft YaHei, sans-serif";
    ctx.fillText("无可视化数据", 16, 28);
}

function drawExplainGexWalls(ctx, width, height, chart) {
    const pad = { top: 28, right: 16, bottom: 28, left: 48 };
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;
    const rows = (chart.strikes || []).filter((row) => Number.isFinite(Number(row.strike)));
    if (!rows.length) {
        ctx.fillStyle = "#8b98a8";
        ctx.font = "13px Microsoft YaHei, sans-serif";
        ctx.fillText("暂无行权价 GEX 数据", 16, 28);
        return;
    }
    const values = rows.flatMap((row) => [Number(row.call_gex || 0), Number(row.put_gex || 0)]);
    const maxAbs = Math.max(1e-9, ...values.map((item) => Math.abs(item)));
    const zeroY = pad.top + innerH / 2;
    const barW = Math.max(2, innerW / rows.length * 0.36);
    rows.forEach((row, index) => {
        const x = pad.left + ((index + 0.5) / rows.length) * innerW;
        const call = Number(row.call_gex || 0);
        const put = Number(row.put_gex || 0);
        const callH = (Math.abs(call) / maxAbs) * (innerH * 0.45);
        const putH = (Math.abs(put) / maxAbs) * (innerH * 0.45);
        ctx.fillStyle = "rgba(255,159,67,0.85)";
        ctx.fillRect(x - barW - 1, zeroY - callH, barW, callH);
        ctx.fillStyle = "rgba(84,160,255,0.85)";
        ctx.fillRect(x + 1, zeroY, barW, putH);
    });
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.beginPath();
    ctx.moveTo(pad.left, zeroY);
    ctx.lineTo(pad.left + innerW, zeroY);
    ctx.stroke();
    const levels = [
        { strike: chart.spot, color: "#e8edf2", label: `现价 ${chart.spot ?? "—"}`, width: 1.4 },
        { strike: chart.call_wall, color: "#ff9f43", label: `Call墙 ${chart.call_wall ?? "—"}`, width: 1.6 },
        { strike: chart.put_wall, color: "#54a0ff", label: `Put墙 ${chart.put_wall ?? "—"}`, width: 1.6 },
    ];
    levels.forEach((level) => {
        const strike = Number(level.strike);
        if (!Number.isFinite(strike)) {
            return;
        }
        const x = strikeToX(strike, rows, pad.left, innerW);
        ctx.save();
        ctx.strokeStyle = level.color;
        ctx.lineWidth = level.width;
        ctx.setLineDash(level.strike === chart.spot ? [4, 4] : []);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + innerH);
        ctx.stroke();
        ctx.fillStyle = level.color;
        ctx.font = "11px Microsoft YaHei, sans-serif";
        ctx.fillText(level.label, Math.min(x + 4, width - 110), pad.top + 12);
        ctx.restore();
    });
}

function drawExplainGauge(ctx, width, height, chart) {
    const min = Number(chart.min || 0);
    const max = Number(chart.max || 1);
    const value = Math.min(max, Math.max(min, Number(chart.value || 0)));
    const cx = width / 2;
    const cy = height * 0.68;
    const radius = Math.min(width, height) * 0.34;
    const start = Math.PI;
    const end = 0;
    ctx.lineWidth = 18;
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.beginPath();
    ctx.arc(cx, cy, radius, start, end);
    ctx.stroke();
    const ratio = max === min ? 0 : (value - min) / (max - min);
    ctx.strokeStyle = "#54a0ff";
    ctx.beginPath();
    ctx.arc(cx, cy, radius, start, start + Math.PI * ratio);
    ctx.stroke();
    if (chart.threshold != null && Number.isFinite(Number(chart.threshold))) {
        const th = (Number(chart.threshold) - min) / (max - min || 1);
        const ang = start + Math.PI * Math.min(1, Math.max(0, th));
        ctx.strokeStyle = "#ff9f43";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ang) * (radius - 14), cy + Math.sin(ang) * (radius - 14));
        ctx.lineTo(cx + Math.cos(ang) * (radius + 10), cy + Math.sin(ang) * (radius + 10));
        ctx.stroke();
    }
    ctx.fillStyle = "#e8edf2";
    ctx.font = "22px Microsoft YaHei, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(String(Number(value.toFixed(4))), cx, cy - 8);
    ctx.fillStyle = "#8b98a8";
    ctx.font = "12px Microsoft YaHei, sans-serif";
    ctx.fillText(chart.label || "", cx, cy + 18);
    ctx.textAlign = "left";
}

function drawExplainBar(ctx, width, height, chart) {
    const pad = 40;
    const max = Math.max(Number(chart.max || 1), 1e-9);
    const value = Number(chart.value || 0);
    const barW = width - pad * 2;
    const barH = 28;
    const y = height / 2 - barH / 2;
    ctx.fillStyle = "rgba(255,255,255,0.08)";
    ctx.fillRect(pad, y, barW, barH);
    ctx.fillStyle = "#1dd1a1";
    ctx.fillRect(pad, y, barW * Math.min(1, Math.max(0, value / max)), barH);
    ctx.fillStyle = "#e8edf2";
    ctx.font = "14px Microsoft YaHei, sans-serif";
    ctx.fillText(`${chart.label || ""} = ${value}`, pad, y - 12);
}

function drawExplainLegs(ctx, width, height, chart) {
    const legs = (chart.legs || []).filter((item) => item && item.strike != null && item.strike !== "");
    const pad = { top: 36, right: 20, bottom: 30, left: 40 };
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;
    if (!legs.length) {
        ctx.fillStyle = "#8b98a8";
        ctx.font = "13px Microsoft YaHei, sans-serif";
        ctx.fillText("暂无腿结构", 16, 28);
        return;
    }
    const strikes = legs.map((item) => Number(item.strike)).filter((item) => Number.isFinite(item));
    if (chart.spot != null) {
        strikes.push(Number(chart.spot));
    }
    const minK = Math.min(...strikes);
    const maxK = Math.max(...strikes);
    const xOf = (strike) => {
        if (maxK === minK) {
            return pad.left + innerW / 2;
        }
        return pad.left + ((Number(strike) - minK) / (maxK - minK)) * innerW;
    };
    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top + innerH * 0.55);
    ctx.lineTo(pad.left + innerW, pad.top + innerH * 0.55);
    ctx.stroke();
    if (Number.isFinite(Number(chart.spot))) {
        const x = xOf(chart.spot);
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = "#e8edf2";
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + innerH);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#e8edf2";
        ctx.font = "11px Microsoft YaHei, sans-serif";
        ctx.fillText(`现价 ${chart.spot}`, x + 4, pad.top + 12);
    }
    legs.forEach((leg, index) => {
        const x = xOf(leg.strike);
        const color = String(leg.name || "").includes("长") ? "#1dd1a1" : "#ff9f43";
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, pad.top + innerH * 0.55, 7, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = "11px Microsoft YaHei, sans-serif";
        ctx.fillText(`${leg.name} ${leg.strike}`, x - 24, pad.top + 28 + index * 14);
    });
    if (chart.credit != null) {
        ctx.fillStyle = "#d7e6ff";
        ctx.font = "13px Microsoft YaHei, sans-serif";
        ctx.fillText(`净权利金 ${chart.credit}`, pad.left, height - 12);
    }
}

let liveMonitorTimer = null;
function scheduleLiveMonitor() {
    if (!state.token || !$("tab-live") || !$("tab-live").classList.contains("active")) {
        return;
    }
    if (liveMonitorTimer) {
        return;
    }
    liveMonitorTimer = setTimeout(async () => {
        liveMonitorTimer = null;
        try {
            await refreshLive();
        } catch (error) {
            appendLog(error.message);
        }
        if (state.token && $("tab-live") && $("tab-live").classList.contains("active")) {
            scheduleLiveMonitor();
        }
    }, 2000);
}

let scriptMonitorTimer = null;
function scheduleScriptMonitor() {
    if (!state.token || !$("tab-script") || !$("tab-script").classList.contains("active")) {
        return;
    }
    if (scriptMonitorTimer) {
        return;
    }
    scriptMonitorTimer = setTimeout(async () => {
        scriptMonitorTimer = null;
        try {
            await refreshScriptMonitor();
        } catch (error) {
            appendLog(error.message);
        }
        if (state.token && $("tab-script") && $("tab-script").classList.contains("active")) {
            scheduleScriptMonitor();
        }
    }, 1000);
}

async function refreshScriptMonitor() {
    const data = await api("/script/monitor");
    renderScriptMonitor(data);
}

function renderScriptMonitor(data) {
    const runBox = $("script-run-metrics");
    const paramBox = $("script-params");
    const hint = $("script-run-hint");
    const logBox = $("script-decision-log");
    if (!runBox || !paramBox) {
        return;
    }
    const active = data.engine_active || data.active;
    const params = data.params || {};
    const books = data.books || {};
    const bookNames = Object.keys(books);
    const firstSnap = bookNames.length ? books[bookNames[0]] : null;
    const book = (firstSnap && firstSnap.book) || data.book || {};
    const isGex = data.iv_rank != null || data.pick || bookNames.length || params.risk_cap != null || params.portfolios;
    if (hint) {
        if (!active) {
            hint.textContent = data.live_supervisor
                ? "守护进程已开启。CTP 连上并初始化 IO.CFFEX 后会自动拉起实盘铁鹰。"
                : "脚本未运行。启动 gex_tv_strangle.py 后可监控开仓与决策。";
        } else if (isGex) {
            hint.textContent = `${data.dry_run ? "模拟" : "实盘"}铁鹰 ｜ ${data.chain || data.portfolio || ""} ｜ IV Rank ${data.iv_rank ?? "—"} ｜ ${data.updated || ""}`;
        } else {
            hint.textContent = `${data.dry_run ? "模拟报价" : "实盘报价"} ｜ ${data.chain_symbol || ""} ｜ 更新 ${data.updated || "—"} ｜ 循环 ${data.loop || 0}`;
        }
    }
    if (isGex) {
        runBox.innerHTML = [
            metricHtml("引擎", active ? "运行中" : "未运行"),
            metricHtml("模式", data.dry_run ? "DRY RUN" : "实盘", data.dry_run ? -1 : 1),
            metricHtml("组合", data.portfolio || (params.portfolios || []).join(" / ") || params.portfolio_name || "—"),
            metricHtml("链", data.chain || "—"),
            metricHtml("标的", data.spot ?? "—"),
            metricHtml("IV Rank", data.iv_rank ?? "—", data.iv_high ? 1 : -1),
            metricHtml("手数", book.lots ?? 0, Number(book.lots || 0)),
            metricHtml("净值份额", data.nav ?? "—"),
            metricHtml("状态", data.reason || "正常", data.reason ? 0 : 1),
        ].join("");
        const gexLabels = {
            portfolio_name: "组合",
            risk_cap: "风险上限",
            roll_dte: "移仓DTE",
            wing_steps: "翼宽档",
            max_lots: "手数上限",
            option_size: "期权乘数",
            dry_run: "模拟",
            capital_share: "资金份额",
        };
        const gexParams = { ...params, ...(firstSnap || {}) };
        const keys = Object.keys(gexLabels).filter((key) => gexParams[key] != null);
        paramBox.innerHTML = keys.length
            ? keys.map((key) => metricHtml(gexLabels[key], Array.isArray(gexParams[key]) ? gexParams[key].join(", ") : gexParams[key])).join("")
            : `<p class="hint">运行后展示风险上限 / 翼宽 / 手数</p>`;
    } else {
        const portfolio = data.portfolio && typeof data.portfolio === "object" ? data.portfolio : {};
        runBox.innerHTML = [
            metricHtml("引擎", active ? "运行中" : "未运行"),
            metricHtml("模式", data.dry_run ? "DRY RUN" : "实盘"),
            metricHtml("链", data.chain_symbol || "—"),
            metricHtml("监控/报价", `${data.watch_count ?? 0} / ${data.quote_count ?? 0}`),
            metricHtml("组合净仓", portfolio.net_pos ?? "—", Number(portfolio.net_pos || 0)),
            metricHtml("Δ", portfolio.pos_delta != null ? Number(portfolio.pos_delta).toFixed(2) : "—", Number(portfolio.pos_delta || 0)),
            metricHtml("Γ", portfolio.pos_gamma != null ? Number(portfolio.pos_gamma).toFixed(4) : "—", Number(portfolio.pos_gamma || 0)),
            metricHtml("Vega", portfolio.pos_vega != null ? Number(portfolio.pos_vega).toFixed(2) : "—", Number(portfolio.pos_vega || 0)),
            metricHtml("状态", data.halted ? (data.halt_reason || "熔断") : (data.error || "正常"), data.halted ? -1 : 1),
        ].join("");
        const labels = {
            gamma: "γ 风险厌恶",
            kappa: "κ 到达强度",
            sigma: "σ 年化波动",
            tau_days: "视野(日)",
            theo_weight: "模型价权重",
            min_spread_ticks: "最小价差(跳)",
            vol_spread: "Vega价差",
            quote_volume: "报单量",
            max_pos: "单合约上限",
            flatten_inventory: "减仓阈值",
            atm_strikes: "ATM档数",
            min_unit_delta: "最小|Δ|",
            max_unit_delta: "最大|Δ|",
            portfolio_name: "组合",
        };
        const keys = Object.keys(labels).filter((key) => params[key] != null);
        paramBox.innerHTML = keys.length
            ? keys.map((key) => metricHtml(labels[key], params[key])).join("")
            : `<p class="hint">运行后展示 gamma / kappa / sigma 等输入</p>`;
    }
    renderTable("script-quote-body", data.quotes || [], (row) => {
        const cls = row.quoting ? "" : "skip";
        return `<tr class="${cls}">
            <td>${row.vt_symbol || ""}</td>
            <td>${row.option_type || ""}</td>
            <td class="${signedClass(row.net_pos)}">${row.net_pos ?? ""}</td>
            <td>${row.unit_delta ?? ""}</td>
            <td>${row.market_bid || "—"}/${row.market_ask || "—"}</td>
            <td>${row.mid || "—"}</td>
            <td>${row.reservation || "—"}</td>
            <td>${row.bid || "—"}/${row.ask || "—"}</td>
            <td>${row.spread || "—"}</td>
            <td>${row.spread_driver || ""}</td>
            <td>${row.action || ""}</td>
            <td>${row.reason || ""}</td>
        </tr>`;
    });
    if (logBox) {
        const lines = data.decisions || [];
        logBox.textContent = lines.length ? lines.join("\n") : "等待策略输出";
        logBox.scrollTop = logBox.scrollHeight;
    }
}

function formatScriptBtNum(value, digits = 2) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
        return "—";
    }
    return number.toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

function isGexEngine() {
    return !$("script-bt-engine") || $("script-bt-engine").value !== "as_mm";
}

function syncScriptBtEngineUi() {
    const gex = isGexEngine();
    if ($("script-bt-gex-fields")) {
        $("script-bt-gex-fields").classList.toggle("hidden", !gex);
    }
    if ($("script-bt-as-fields")) {
        $("script-bt-as-fields").classList.toggle("hidden", gex);
    }
    if ($("script-bt-opt-card")) {
        $("script-bt-opt-card").classList.toggle("hidden", gex);
    }
    if ($("script-bt-as-panes")) {
        $("script-bt-as-panes").classList.toggle("hidden", gex);
    }
    if ($("script-bt-trades-wrap")) {
        $("script-bt-trades-wrap").classList.toggle("hidden", !gex);
    }
}

function scriptBtQuery() {
    if (isGexEngine()) {
        const kind = $("script-bt-kind") ? $("script-bt-kind").value : "SA";
        const interval = $("script-bt-interval") ? $("script-bt-interval").value : "1d";
        return `?engine=gex&kind=${encodeURIComponent(kind)}&interval=${encodeURIComponent(interval)}`;
    }
    return "?engine=as_mm";
}

function scriptBtPayload() {
    if (isGexEngine()) {
        const preset = $("script-bt-preset") ? $("script-bt-preset").value : "自定义参数";
        return {
            engine: "gex",
            kind: $("script-bt-kind").value,
            interval: $("script-bt-interval").value,
            name: preset === "自定义参数" ? "自定义参数" : preset,
            risk_cap: Number($("script-bt-risk").value),
            roll_dte: Number($("script-bt-roll").value),
            max_lots: Number($("script-bt-maxlots").value),
            iv_rank_min: Number($("script-bt-ivrank").value),
            compare: $("script-bt-compare").checked,
            hedge: false,
        };
    }
    const preset = $("script-bt-as-preset") ? $("script-bt-as-preset").value : "自定义参数";
    return {
        engine: "as_mm",
        name: preset === "自定义参数" ? "自定义参数" : preset,
        gamma: Number($("script-bt-gamma").value),
        kappa: Number($("script-bt-kappa").value),
        spread_mult: Number($("script-bt-spread").value),
        sigma_floor: Number($("script-bt-sigma").value),
        tau_days: Number($("script-bt-tau").value),
        max_pos: Number($("script-bt-maxpos").value),
        hedge: $("script-bt-hedge").checked,
        compare: $("script-bt-compare").checked,
    };
}

function scriptBtOptimizePayload() {
    return Object.assign(scriptBtPayload(), {
        objective: $("script-bt-opt-objective").value,
        hedge_mode: $("script-bt-opt-hedge").value,
        gamma_start: Number($("script-bt-opt-gamma-start").value),
        gamma_end: Number($("script-bt-opt-gamma-end").value),
        gamma_step: Number($("script-bt-opt-gamma-step").value),
        kappa_start: Number($("script-bt-opt-kappa-start").value),
        kappa_end: Number($("script-bt-opt-kappa-end").value),
        kappa_step: Number($("script-bt-opt-kappa-step").value),
        spread_start: Number($("script-bt-opt-spread-start").value),
        spread_end: Number($("script-bt-opt-spread-end").value),
        spread_step: Number($("script-bt-opt-spread-step").value),
        tau_start: Number($("script-bt-opt-tau-start").value),
        tau_end: Number($("script-bt-opt-tau-end").value),
        tau_step: Number($("script-bt-opt-tau-step").value),
    });
}

function applyScriptBtRow(row) {
    if (!row) {
        return;
    }
    if (isGexEngine()) {
        if ($("script-bt-preset")) {
            $("script-bt-preset").value = "自定义参数";
        }
        if (row.risk_cap != null && $("script-bt-risk")) {
            $("script-bt-risk").value = row.risk_cap;
        }
        if (row.roll_dte != null && $("script-bt-roll")) {
            $("script-bt-roll").value = row.roll_dte;
        }
        if (row.max_lots != null && $("script-bt-maxlots")) {
            $("script-bt-maxlots").value = row.max_lots;
        }
        if (row.iv_rank_min != null && $("script-bt-ivrank")) {
            $("script-bt-ivrank").value = row.iv_rank_min;
        }
        return;
    }
    if ($("script-bt-as-preset")) {
        $("script-bt-as-preset").value = "自定义参数";
    }
    if (row.gamma != null) {
        $("script-bt-gamma").value = row.gamma;
    }
    if (row.kappa != null) {
        $("script-bt-kappa").value = row.kappa;
    }
    if (row.spread_mult != null) {
        $("script-bt-spread").value = row.spread_mult;
    }
    if (row.sigma_floor != null) {
        $("script-bt-sigma").value = row.sigma_floor;
    }
    if (row.tau_days != null) {
        $("script-bt-tau").value = row.tau_days;
    }
    if (row.max_pos != null) {
        $("script-bt-maxpos").value = row.max_pos;
    }
    if (row.hedge != null) {
        $("script-bt-hedge").checked = !!row.hedge;
    }
}

function applyScriptBtPreset(name) {
    if (!name || name === "自定义参数") {
        return;
    }
    const preset = (state.scriptBtPresets || []).find((item) => item.name === name);
    if (!preset) {
        return;
    }
    if (isGexEngine()) {
        if (preset.risk_cap != null) {
            $("script-bt-risk").value = preset.risk_cap;
        }
        if (preset.roll_dte != null) {
            $("script-bt-roll").value = preset.roll_dte;
        }
        if (preset.max_lots != null) {
            $("script-bt-maxlots").value = preset.max_lots;
        }
        if (preset.iv_rank_min != null) {
            $("script-bt-ivrank").value = preset.iv_rank_min;
        }
        return;
    }
    $("script-bt-gamma").value = preset.gamma;
    $("script-bt-kappa").value = preset.kappa;
    $("script-bt-spread").value = preset.spread_mult;
    $("script-bt-sigma").value = preset.sigma_floor;
    $("script-bt-tau").value = preset.tau_days;
    $("script-bt-maxpos").value = preset.max_pos;
    $("script-bt-hedge").checked = !!preset.hedge;
}

function fillScriptBtPresets(presets) {
    const select = isGexEngine() ? $("script-bt-preset") : $("script-bt-as-preset");
    if (!select) {
        return;
    }
    const key = isGexEngine()
        ? `gex|${$("script-bt-kind") ? $("script-bt-kind").value : "SA"}|${$("script-bt-interval") ? $("script-bt-interval").value : "1d"}`
        : "as_mm";
    const fallback = isGexEngine() ? GEX_FALLBACK_PRESETS : AS_FALLBACK_PRESETS;
    state.scriptBtPresets = (presets && presets.length) ? presets : fallback;
    if (state.scriptBtPresetKey === key && select.options.length > 1) {
        return;
    }
    state.scriptBtPresetKey = key;
    const names = ["自定义参数"].concat(state.scriptBtPresets.map((item) => item.name));
    const preferred = state.scriptBtPresets[0] ? state.scriptBtPresets[0].name : "自定义参数";
    fillSelect(select, names, preferred);
    applyScriptBtPreset(preferred);
}

function selectedScriptBtResult(data) {
    const results = ((data && data.result) || {}).results || [];
    if (!results.length) {
        return null;
    }
    const pick = $("script-bt-pick");
    const name = pick && pick.value;
    return results.find((item) => item.name === name) || results[0];
}

function fillScriptBtPick(results, selectedName) {
    const pick = $("script-bt-pick");
    if (!pick) {
        return;
    }
    const names = (results || []).map((item) => item.name);
    fillSelect(pick, names, names.includes(selectedName) ? selectedName : names[0] || "");
    pick.style.display = names.length > 1 && names.length <= 8 ? "" : "none";
}

function drawScriptBtChart(row) {
    const canvas = $("script-bt-chart");
    if (!canvas) {
        return;
    }
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 180);
    const xs = (row && row.equity_x) || [];
    const ys = ((row && row.equity_y) || []).map((value) => Number(value));
    if (!xs.length || ys.length < 2) {
        ctx.fillStyle = "#8b98a6";
        ctx.font = "12px Microsoft YaHei, sans-serif";
        ctx.fillText("回测完成后显示净值曲线", 16, cssHeight / 2);
        return;
    }
    drawScriptBtLinePane(ctx, cssWidth, cssHeight, xs, [{ values: ys, color: "#3d8bfd", name: "净值" }], {
        includeZero: true,
        digits: 0,
    });
}

function scriptBtChartOf(row) {
    return (row && row.chart) || {};
}

function drawScriptBtEmpty(canvas, height, text) {
    if (!canvas) {
        return;
    }
    const { ctx, cssHeight } = setupCanvas(canvas, height);
    ctx.fillStyle = "#8b98a6";
    ctx.font = "12px Microsoft YaHei, sans-serif";
    ctx.fillText(text, 16, cssHeight / 2);
}

function scriptBtXOf(n, pad, innerW) {
    return (index) => pad.left + (n <= 1 ? innerW / 2 : index / (n - 1) * innerW);
}

function scriptBtYOf(minY, maxY, pad, innerH) {
    const span = Math.max(maxY - minY, 1e-9);
    return (value) => pad.top + (maxY - value) / span * innerH;
}

function scriptBtRange(values, includeZero) {
    const nums = (values || []).map((value) => Number(value)).filter((value) => Number.isFinite(value));
    if (!nums.length) {
        return includeZero ? [-1, 1] : [0, 1];
    }
    let minY = Math.min(...nums);
    let maxY = Math.max(...nums);
    if (includeZero) {
        minY = Math.min(0, minY);
        maxY = Math.max(0, maxY);
    }
    const pad = Math.max((maxY - minY) * 0.08, Math.abs(maxY) * 0.002, 1e-6);
    return [minY - pad, maxY + pad];
}

function drawScriptBtFrame(ctx, pad, cssWidth, cssHeight, xs, leftLabel, rightLabel) {
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    ctx.strokeStyle = "#2b3642";
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, pad.top + innerH);
    ctx.lineTo(pad.left + innerW, pad.top + innerH);
    if (rightLabel) {
        ctx.moveTo(pad.left + innerW, pad.top);
        ctx.lineTo(pad.left + innerW, pad.top + innerH);
    }
    ctx.stroke();
    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    if (xs && xs.length) {
        ctx.fillText(String(xs[0]), pad.left, cssHeight - 6);
        ctx.textAlign = "right";
        ctx.fillText(String(xs[xs.length - 1]), cssWidth - pad.right, cssHeight - 6);
        ctx.textAlign = "left";
    }
    if (leftLabel) {
        ctx.fillText(leftLabel, 4, pad.top + 8);
    }
    if (rightLabel) {
        ctx.textAlign = "right";
        ctx.fillText(rightLabel, cssWidth - 4, pad.top + 8);
        ctx.textAlign = "left";
    }
}

function drawScriptBtSeries(ctx, values, xOf, yOf, color, width, dash) {
    if (!values || values.length < 2) {
        return;
    }
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1.5;
    if (dash) {
        ctx.setLineDash(dash);
    }
    ctx.beginPath();
    let started = false;
    values.forEach((raw, index) => {
        const value = Number(raw);
        if (!Number.isFinite(value)) {
            started = false;
            return;
        }
        const x = xOf(index);
        const y = yOf(value);
        if (!started) {
            ctx.moveTo(x, y);
            started = true;
        } else {
            ctx.lineTo(x, y);
        }
    });
    ctx.stroke();
    ctx.restore();
}

function drawScriptBtLegend(ctx, items, pad) {
    let x = pad.left + 108;
    const y = 14;
    ctx.font = "11px Microsoft YaHei, sans-serif";
    items.forEach((item) => {
        ctx.fillStyle = item.color;
        if (item.mark === "up") {
            drawScriptBtTriangle(ctx, x + 5, y - 1, true, item.color, 5);
        } else if (item.mark === "down") {
            drawScriptBtTriangle(ctx, x + 5, y - 1, false, item.color, 5);
        } else if (item.mark === "diamond") {
            drawScriptBtDiamond(ctx, x + 5, y - 1, item.color, 4);
        } else {
            ctx.fillRect(x, y - 6, 10, 3);
        }
        ctx.fillStyle = "#8b98a6";
        ctx.fillText(item.name, x + 14, y);
        x += ctx.measureText(item.name).width + 28;
    });
}

function drawScriptBtTriangle(ctx, x, y, up, color, size) {
    ctx.fillStyle = color;
    ctx.beginPath();
    if (up) {
        ctx.moveTo(x, y - size);
        ctx.lineTo(x - size, y + size * 0.6);
        ctx.lineTo(x + size, y + size * 0.6);
    } else {
        ctx.moveTo(x, y + size);
        ctx.lineTo(x - size, y - size * 0.6);
        ctx.lineTo(x + size, y - size * 0.6);
    }
    ctx.closePath();
    ctx.fill();
}

function drawScriptBtDiamond(ctx, x, y, color, size) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, y - size);
    ctx.lineTo(x + size, y);
    ctx.lineTo(x, y + size);
    ctx.lineTo(x - size, y);
    ctx.closePath();
    ctx.fill();
}

function drawScriptBtLinePane(ctx, cssWidth, cssHeight, xs, series, options) {
    const pad = { left: 52, right: options && options.rightAxis ? 52 : 12, top: 20, bottom: 22 };
    const innerW = cssWidth - pad.left - pad.right;
    const innerH = cssHeight - pad.top - pad.bottom;
    const leftValues = [];
    (series || []).filter((item) => !item.right).forEach((item) => leftValues.push(...item.values));
    const [minL, maxL] = scriptBtRange(leftValues, !!(options && options.includeZero));
    const xOf = scriptBtXOf((xs || []).length, pad, innerW);
    const yLeft = scriptBtYOf(minL, maxL, pad, innerH);
    const rightSeries = (series || []).filter((item) => item.right);
    let yRight = yLeft;
    let minR = minL;
    let maxR = maxL;
    if (rightSeries.length) {
        const rightValues = [];
        rightSeries.forEach((item) => rightValues.push(...item.values));
        [minR, maxR] = scriptBtRange(rightValues, true);
        yRight = scriptBtYOf(minR, maxR, pad, innerH);
    }
    drawScriptBtFrame(
        ctx,
        pad,
        cssWidth,
        cssHeight,
        xs,
        formatScriptBtNum(maxL, options && options.digits != null ? options.digits : 2),
        rightSeries.length ? formatScriptBtNum(maxR, 2) : ""
    );
    ctx.fillStyle = "#8b98a6";
    ctx.font = "11px Microsoft YaHei, sans-serif";
    ctx.fillText(formatScriptBtNum(minL, options && options.digits != null ? options.digits : 2), 4, pad.top + innerH);
    if (rightSeries.length) {
        ctx.textAlign = "right";
        ctx.fillText(formatScriptBtNum(minR, 2), cssWidth - 4, pad.top + innerH);
        ctx.textAlign = "left";
    }
    if (minL < 0 && maxL > 0) {
        ctx.strokeStyle = "#3a4552";
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(pad.left, yLeft(0));
        ctx.lineTo(pad.left + innerW, yLeft(0));
        ctx.stroke();
        ctx.setLineDash([]);
    }
    series.forEach((item) => {
        drawScriptBtSeries(ctx, item.values, xOf, item.right ? yRight : yLeft, item.color, item.width || 1.5, item.dash);
    });
    if (options && options.legend) {
        drawScriptBtLegend(ctx, options.legend, pad);
    }
    return { pad, innerW, innerH, xOf, yLeft, yRight };
}

function drawScriptBtPricePane(chart) {
    const canvas = $("script-bt-price");
    const xs = (chart && chart.x) || [];
    const spots = (chart && chart.spot) || [];
    if (!canvas) {
        return;
    }
    if (spots.length < 2) {
        drawScriptBtEmpty(canvas, 280, "请重新回测以生成价格与成交图");
        return;
    }
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 280);
    const layout = drawScriptBtLinePane(ctx, cssWidth, cssHeight, xs, [
        { values: spots, color: "#d7dee7", name: "标的", width: 1.6 },
    ], {
        digits: 0,
        legend: [
            { name: "标的", color: "#d7dee7" },
            { name: "买入", color: "#2ecc71", mark: "up" },
            { name: "卖出", color: "#ff6b6b", mark: "down" },
            { name: "对冲", color: "#f0b429", mark: "diamond" },
        ],
    });
    const trades = chart.trades || [];
    trades.forEach((trade) => {
        const index = Number(trade.i);
        const spot = Number(spots[index]);
        if (!Number.isFinite(spot)) {
            return;
        }
        const x = layout.xOf(index);
        const y = layout.yLeft(spot);
        const size = Math.min(7, 3 + Math.log2(Number(trade.n || 1) + 1));
        if (trade.k === "F") {
            drawScriptBtDiamond(ctx, x, y, "#f0b429", size);
            return;
        }
        drawScriptBtTriangle(ctx, x, y, Number(trade.s) > 0, Number(trade.s) > 0 ? "#2ecc71" : "#ff6b6b", size);
    });
}

function drawScriptBtInvPane(chart) {
    const canvas = $("script-bt-inv");
    if (!canvas) {
        return;
    }
    const xs = (chart && chart.x) || [];
    if (!xs.length) {
        drawScriptBtEmpty(canvas, 156, "等待库存序列");
        return;
    }
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 156);
    drawScriptBtLinePane(ctx, cssWidth, cssHeight, xs, [
        { values: chart.call || [], color: "#ff9f43", width: 1.4 },
        { values: chart.put || [], color: "#54a0ff", width: 1.4 },
        { values: chart.net || [], color: "#e8edf2", width: 1.6 },
        { values: chart.fut || [], color: "#f0b429", width: 1.2, dash: [4, 3] },
        { values: chart.delta || [], color: "#b197fc", width: 1.2, right: true },
    ], {
        includeZero: true,
        digits: 0,
        rightAxis: true,
        legend: [
            { name: "Call仓", color: "#ff9f43" },
            { name: "Put仓", color: "#54a0ff" },
            { name: "净仓", color: "#e8edf2" },
            { name: "IF仓", color: "#f0b429" },
            { name: "Δ", color: "#b197fc" },
        ],
    });
}

function drawScriptBtDecisionPane(chart) {
    const canvas = $("script-bt-decision");
    if (!canvas) {
        return;
    }
    const xs = (chart && chart.x) || [];
    if (!xs.length) {
        drawScriptBtEmpty(canvas, 156, "等待决策参数序列");
        return;
    }
    const { ctx, cssWidth, cssHeight } = setupCanvas(canvas, 156);
    drawScriptBtLinePane(ctx, cssWidth, cssHeight, xs, [
        { values: chart.sigma || [], color: "#b197fc", width: 1.5 },
        { values: chart.spread || [], color: "#3d8bfd", width: 1.4, right: true },
        { values: chart.bias || [], color: "#ff6b6b", width: 1.2, right: true, dash: [5, 3] },
    ], {
        includeZero: true,
        digits: 2,
        rightAxis: true,
        legend: [
            { name: "σ", color: "#b197fc" },
            { name: "报价价差", color: "#3d8bfd" },
            { name: "保留价偏离", color: "#ff6b6b" },
        ],
    });
}

function drawScriptBtTradeCharts(row) {
    if (!isGexEngine()) {
        const chart = scriptBtChartOf(row);
        drawScriptBtPricePane(chart);
        drawScriptBtInvPane(chart);
        drawScriptBtDecisionPane(chart);
    }
    drawScriptBtChart(row);
}

function renderScriptBacktest(data) {
    state.scriptBacktest = data;
    syncScriptBtEngineUi();
    const gex = isGexEngine();
    const status = $("script-bt-status");
    const hint = $("script-bt-hint");
    const metrics = $("script-bt-metrics");
    const startBtn = $("script-bt-start");
    const cacheBtn = $("script-bt-cache");
    const optBtn = $("script-bt-opt-start");
    const optStatus = $("script-bt-opt-status");
    const result = (data && data.result) || {};
    const cache = (data && data.cache) || {};
    const progress = (data && data.progress) || {};
    const optimize = result.optimize || {};
    const results = result.results || [];
    const sample = result.sample || {};
    fillScriptBtPresets(data.presets || []);
    if (startBtn) {
        startBtn.disabled = !!data.running;
    }
    if (cacheBtn) {
        cacheBtn.disabled = !!data.running;
    }
    if (optBtn) {
        optBtn.disabled = !!data.running;
    }
    if (status) {
        if (data.running) {
            status.textContent = data.message || (data.phase === "cache" ? "正在刷新行情缓存…" : "正在回测…");
        } else if (data.error) {
            status.textContent = data.error;
        } else if (cache.exists) {
            status.textContent = `缓存 ${cache.start || "—"} ~ ${cache.end || "—"} ｜ ${cache.bars || 0} 根 ｜ ${cache.days || 0} 天`;
        } else {
            status.textContent = cache.error || (gex ? "缺少行情缓存，请先刷新" : "缺少 30 分钟缓存，请先刷新行情");
        }
    }
    if (optStatus) {
        if (data.running && data.phase === "optimize") {
            const total = progress.total || 0;
            const done = progress.done || 0;
            optStatus.textContent = total
                ? `寻优 ${done}/${total}${progress.best ? ` ｜ 最佳 ${progress.best}` : ""}`
                : (data.message || "正在寻优…");
        } else if (optimize.combos) {
            optStatus.textContent = `${optimize.objective_label || "寻优"} ${optimize.combos} 组${optimize.best_name ? ` ｜ ${optimize.best_name}` : ""}`;
        } else {
            optStatus.textContent = "网格搜索 γ / κ / 价差 / 视野，上限 120 组";
        }
    }
    const previousPick = $("script-bt-pick") ? $("script-bt-pick").value : "";
    fillScriptBtPick(results, previousPick);
    const row = selectedScriptBtResult(data);
    if (hint) {
        if (data.running) {
            hint.textContent = data.message || "任务进行中";
        } else if (data.error) {
            hint.textContent = data.error;
        } else if (row) {
            const extra = optimize.combos
                ? ` ｜ ${optimize.objective_label || "寻优"} ${optimize.combos} 组，前 5 名含成交图`
                : "";
            hint.textContent = `${result.universe || (gex ? "GEX 铁鹰" : "AS 期权做市")} ｜ ${sample.start || row.start || "—"} ~ ${sample.end || row.end || "—"} ｜ ${sample.bars || row.bars || 0} 根 ｜ ${result.generated || ""}${extra}`;
        } else if (!cache.exists) {
            hint.textContent = gex ? "缺少行情缓存，请先点击「刷新行情缓存」" : "缺少沪深300 30分钟缓存，请先点击「刷新行情缓存」";
        } else {
            hint.textContent = "选择参数后点击「开始回测」。也可勾选「跑全部预设对比」。";
        }
    }
    if (metrics) {
        metrics.innerHTML = row
            ? (gex
                ? [
                    metricHtml("方案", row.name || "—"),
                    metricHtml("年化", `${row.cagr ?? "—"}%`, Number(row.cagr || 0)),
                    metricHtml("盈亏", formatScriptBtNum(row.final_pnl), Number(row.final_pnl || 0)),
                    metricHtml("夏普", formatScriptBtNum(row.sharpe, 3), Number(row.sharpe || 0)),
                    metricHtml("峰值回撤", `${row.max_dd_peak_pct ?? "—"}%`, -Math.abs(Number(row.max_dd_peak_pct || 0))),
                    metricHtml("开仓", row.opens ?? "—"),
                    metricHtml("止盈 / 止损", `${row.take_profits ?? 0} / ${row.stops ?? 0}`),
                    metricHtml("移仓", row.rolls ?? "—"),
                    metricHtml("手数", `${row.min_open_lots ?? "—"}–${row.max_open_lots ?? "—"}`),
                    metricHtml("持仓天数", row.open_days ?? "—"),
                ].join("")
                : [
                    metricHtml("方案", row.name || "—"),
                    metricHtml("盈亏", formatScriptBtNum(row.final_pnl), Number(row.final_pnl || 0)),
                    metricHtml("夏普", formatScriptBtNum(row.sharpe, 3), Number(row.sharpe || 0)),
                    metricHtml("最大回撤", formatScriptBtNum(row.max_dd), -Math.abs(Number(row.max_dd || 0))),
                    metricHtml("胜率", `${row.win_rate ?? "—"}%`),
                    metricHtml("成交", row.fills ?? "—"),
                    metricHtml("日均", formatScriptBtNum(row.daily_mean), Number(row.daily_mean || 0)),
                    metricHtml("日波动", formatScriptBtNum(row.daily_std)),
                    metricHtml("最好/最差", `${formatScriptBtNum(row.best_day, 0)} / ${formatScriptBtNum(row.worst_day, 0)}`),
                    metricHtml("对冲", row.hedge ? "IF" : "无"),
                ].join(""))
            : `<p class="hint">等待回测完成</p>`;
    }
    const head = $("script-bt-compare-head");
    if (head) {
        head.innerHTML = gex
            ? "<th>方案</th><th>年化</th><th>盈亏</th><th>夏普</th><th>回撤</th><th>开仓</th><th>止盈</th><th>移仓</th><th>手数</th>"
            : "<th>方案</th><th>γ</th><th>κ</th><th>价差</th><th>τ</th><th>盈亏</th><th>夏普</th><th>回撤</th><th>得分</th><th>成交</th><th>对冲</th>";
    }
    drawScriptBtTradeCharts(row);
    if (gex) {
        renderTable("script-bt-body", results, (item) => `
            <tr class="${row && item.name === row.name ? "selected" : ""}" data-name="${item.name}">
                <td>${item.name || ""}</td>
                <td>${item.cagr ?? ""}%</td>
                <td class="${signedClass(item.final_pnl)}">${formatScriptBtNum(item.final_pnl)}</td>
                <td>${formatScriptBtNum(item.sharpe, 3)}</td>
                <td class="sell">${item.max_dd_peak_pct ?? ""}%</td>
                <td>${item.opens ?? ""}</td>
                <td>${item.take_profits ?? ""}</td>
                <td>${item.rolls ?? ""}</td>
                <td>${item.min_open_lots ?? ""}–${item.max_open_lots ?? ""}</td>
            </tr>`);
        const trades = ((row && row.trades) || []).slice().reverse();
        if ($("script-bt-trades")) {
            renderTable("script-bt-trades", trades, (item) => `
                <tr>
                    <td>${item.date || ""}</td>
                    <td>${item.action || ""}</td>
                    <td>${item.k_put ?? ""}</td>
                    <td>${item.k_call ?? ""}</td>
                    <td>${item.lots ?? ""}</td>
                    <td>${item.credit != null ? item.credit : (item.debit ?? "")}</td>
                    <td>${item.dte ?? ""}</td>
                </tr>`);
        }
    } else {
        renderTable("script-bt-body", results, (item) => `
            <tr class="${row && item.name === row.name ? "selected" : ""}" data-name="${item.name}">
                <td>${item.name || ""}</td>
                <td>${item.gamma ?? ""}</td>
                <td>${item.kappa ?? ""}</td>
                <td>${item.spread_mult ?? ""}</td>
                <td>${item.tau_days ?? ""}</td>
                <td class="${signedClass(item.final_pnl)}">${formatScriptBtNum(item.final_pnl)}</td>
                <td>${formatScriptBtNum(item.sharpe, 3)}</td>
                <td class="sell">${formatScriptBtNum(item.max_dd)}</td>
                <td>${item.score != null ? formatScriptBtNum(item.score, 3) : "—"}</td>
                <td>${item.fills ?? ""}</td>
                <td>${item.hedge ? "是" : "否"}</td>
            </tr>`);
    }
}

async function refreshScriptBacktest() {
    if (!$("script-bt-status")) {
        return;
    }
    syncScriptBtEngineUi();
    const data = await api(`/script/backtest${scriptBtQuery()}`);
    renderScriptBacktest(data);
}

let scriptBtTimer = null;
function scheduleScriptBtPoll() {
    if (!state.token || !$("tab-script") || !$("tab-script").classList.contains("active")) {
        return;
    }
    if (scriptBtTimer) {
        return;
    }
    scriptBtTimer = setTimeout(async () => {
        scriptBtTimer = null;
        try {
            await refreshScriptBacktest();
        } catch (error) {
            appendLog(error.message);
        }
        if (state.token && state.scriptBacktest && state.scriptBacktest.running && $("tab-script") && $("tab-script").classList.contains("active")) {
            scheduleScriptBtPoll();
        }
    }, 800);
}

$("opt-portfolio").addEventListener("change", refreshOption);
$("opt-chain").addEventListener("change", refreshOptionChain);
$("opt-gex-mode").addEventListener("change", () => {
    if (lastOptionChain) {
        renderOptionChainView(lastOptionChain);
    }
});
window.addEventListener("resize", () => {
    if (lastOptionChain && $("tab-option") && $("tab-option").classList.contains("active")) {
        renderGexChart(lastOptionChain.gex || {});
        renderTvYieldChart(lastOptionChain);
        renderIvSmileChart(lastOptionChain);
    }
    if (state.futuresCurve && $("tab-futures") && $("tab-futures").classList.contains("active")) {
        renderFuturesCharts(state.futuresCurve);
        renderFuturesCapitalCharts(state.futuresCurve);
    }
    if (state.scriptBacktest && $("tab-script") && $("tab-script").classList.contains("active")) {
        drawScriptBtTradeCharts(selectedScriptBtResult(state.scriptBacktest));
    }
});

$("fut-search").addEventListener("input", fillFuturesProductSelect);
$("fut-product").addEventListener("change", subscribeAndRefreshFutures);
$("fut-sub-btn").addEventListener("click", subscribeAndRefreshFutures);
$("fut-capital-mode").addEventListener("change", () => {
    if (state.futuresCurve) {
        renderFuturesCurve(state.futuresCurve);
    }
});
$("fut-capital-month").addEventListener("change", () => {
    if (state.futuresCurve) {
        renderFuturesCapitalCharts(state.futuresCurve);
    }
});

$("opt-save-btn").addEventListener("click", async () => {
    const name = currentPortfolio();
    if (!name) {
        appendLog("请先选择期权组合");
        return;
    }
    try {
        const result = await api("/option/portfolio/setting", {
            method: "POST",
            json: {
                portfolio_name: name,
                model_name: $("opt-model").value,
                interest_rate: Number($("opt-rate").value),
                precision: Number($("opt-precision").value),
                chain_underlying_map: parseMap($("opt-map").value),
            },
        });
        appendLog(result.message);
        await refreshOption();
    } catch (error) {
        appendLog(error.message);
    }
});

$("opt-init-btn").addEventListener("click", async () => {
    try {
        const result = await api(`/option/portfolio/${encodeURIComponent(currentPortfolio())}/init`, { method: "POST" });
        appendLog(result.message);
        await refreshOption();
    } catch (error) {
        appendLog(error.message);
    }
});

$("opt-record-btn").addEventListener("click", async () => {
    try {
        const result = await api("/recorder/chain", {
            method: "POST",
            json: {
                portfolio_name: currentPortfolio(),
                chain_symbol: $("opt-chain").value,
                tick: true,
                bar: false,
            },
        });
        appendLog(result.message);
        if ($("opt-record-status")) {
            $("opt-record-status").textContent = result.message;
        }
        renderRecorder(result);
    } catch (error) {
        appendLog(error.message);
    }
});

if ($("opt-record-universe-btn")) {
    $("opt-record-universe-btn").addEventListener("click", async () => {
        try {
            await enrollTickUniverse();
            await refreshRecorder();
        } catch (error) {
            appendLog(error.message);
        }
    });
}

$("opt-hedge-start").addEventListener("click", async () => {
    try {
        const result = await api("/option/hedge/start", {
            method: "POST",
            json: {
                portfolio_name: currentPortfolio(),
                vt_symbol: $("opt-hedge-symbol").value,
                delta_target: Number($("opt-delta-target").value),
                delta_range: Number($("opt-delta-range").value),
            },
        });
        appendLog(result.message);
        await refreshOption();
    } catch (error) {
        appendLog(error.message);
    }
});

$("opt-hedge-stop").addEventListener("click", async () => {
    try {
        appendLog((await api("/option/hedge/stop", { method: "POST" })).message);
        await refreshOption();
    } catch (error) {
        appendLog(error.message);
    }
});

$("sp-add-btn").addEventListener("click", async () => {
    try {
        const result = await api("/spread", {
            method: "POST",
            json: {
                name: $("sp-name").value,
                price_formula: $("sp-formula").value,
                active_symbol: $("sp-active").value,
                min_volume: Number($("sp-minvol").value),
                legs: [
                    { vt_symbol: $("sp-leg-a").value, variable: "A", trading_direction: 1, trading_multiplier: 1 },
                    { vt_symbol: $("sp-leg-b").value, variable: "B", trading_direction: -1, trading_multiplier: 1 },
                ],
            },
        });
        appendLog(result.message);
        await refreshSpread();
    } catch (error) {
        appendLog(error.message);
    }
});

$("sp-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-spread-del]");
    if (!button) {
        return;
    }
    try {
        appendLog((await api(`/spread/${encodeURIComponent(button.dataset.spreadDel)}`, { method: "DELETE" })).message);
        delete state.spreads[button.dataset.spreadDel];
        await refreshSpread();
    } catch (error) {
        appendLog(error.message);
    }
});

$("sp-algo-start").addEventListener("click", async () => {
    try {
        const result = await api("/spread/algo", {
            method: "POST",
            json: {
                spread_name: $("sp-algo-name").value,
                direction: $("sp-algo-dir").value,
                price: Number($("sp-algo-price").value),
                volume: Number($("sp-algo-volume").value),
                payup: Number($("sp-algo-payup").value),
            },
        });
        appendLog(`算法已启动 ${result.algoid}`);
    } catch (error) {
        appendLog(error.message);
    }
});

$("sp-class").addEventListener("change", async () => {
    paramFields("sp-params", await api(`/spread/class/${$("sp-class").value}`));
});

$("sp-stg-add").addEventListener("click", async () => {
    try {
        const result = await api("/spread/strategy", {
            method: "POST",
            json: {
                class_name: $("sp-class").value,
                strategy_name: $("sp-stg-name").value,
                spread_name: $("sp-stg-spread").value,
                setting: readParams("sp-params"),
            },
        });
        appendLog(result.message);
        await refreshSpread();
    } catch (error) {
        appendLog(error.message);
    }
});

$("sp-stg-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-spstg]");
    if (!button) {
        return;
    }
    const name = button.dataset.name;
    const action = button.dataset.spstg;
    try {
        if (action === "remove") {
            await api(`/spread/strategy/${encodeURIComponent(name)}`, { method: "DELETE" });
        } else {
            await api(`/spread/strategy/${encodeURIComponent(name)}/${action}`, { method: "POST" });
        }
        await refreshSpread();
    } catch (error) {
        appendLog(error.message);
    }
});

$("live-save-config").addEventListener("click", async () => {
    try {
        const result = await api("/live/config", { method: "PUT", json: readLiveConfig() });
        appendLog(result.message);
        await refreshLive();
    } catch (error) {
        appendLog(error.message);
    }
});

$("live-start").addEventListener("click", async () => {
    try {
        await api("/live/config", { method: "PUT", json: { ...readLiveConfig(), paused: false, enabled: true } });
        const result = await api("/live/start", { method: "POST" });
        appendLog(result.message);
        await refreshLive();
        scheduleLiveMonitor();
    } catch (error) {
        appendLog(error.message);
    }
});

$("live-stop").addEventListener("click", async () => {
    try {
        const result = await api("/live/stop", { method: "POST" });
        appendLog(result.message);
        await refreshLive();
    } catch (error) {
        appendLog(error.message);
    }
});

$("live-pause").addEventListener("click", async () => {
    try {
        const result = await api("/live/pause", { method: "POST" });
        appendLog(result.message);
        await refreshLive();
    } catch (error) {
        appendLog(error.message);
    }
});

$("live-resume").addEventListener("click", async () => {
    try {
        const result = await api("/live/resume", { method: "POST" });
        appendLog(result.message);
        await refreshLive();
        scheduleLiveMonitor();
    } catch (error) {
        appendLog(error.message);
    }
});

$("live-refresh").addEventListener("click", async () => {
    try {
        await refreshLive();
        appendLog("实盘监控已刷新");
    } catch (error) {
        appendLog(error.message);
    }
});

function bindLiveExplainClicks(rootId) {
    const root = $(rootId);
    if (!root) {
        return;
    }
    root.addEventListener("click", (event) => {
        const metric = event.target.closest("[data-explain]");
        if (!metric || !root.contains(metric)) {
            return;
        }
        openLiveExplainModal(metric.dataset.explain);
    });
    root.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }
        const metric = event.target.closest("[data-explain]");
        if (!metric || !root.contains(metric)) {
            return;
        }
        event.preventDefault();
        openLiveExplainModal(metric.dataset.explain);
    });
}

bindLiveExplainClicks("live-indicators");
bindLiveExplainClicks("live-run-metrics");
bindLiveExplainClicks("live-book");

if ($("live-explain-close")) {
    $("live-explain-close").addEventListener("click", closeLiveExplainModal);
}
if ($("live-explain-modal")) {
    $("live-explain-modal").addEventListener("click", (event) => {
        if (event.target && event.target.dataset && event.target.dataset.explainClose) {
            closeLiveExplainModal();
        }
    });
}
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
        closeLiveExplainModal();
    }
});

$("script-upload-btn").addEventListener("click", async () => {
    const file = $("script-upload").files[0];
    if (!file) {
        appendLog("请选择 .py 文件");
        return;
    }
    const form = new FormData();
    form.append("file", file);
    try {
        const result = await api("/script/upload", { method: "POST", body: form });
        appendLog(result.message);
        $("script-path").value = result.path;
        await refreshScript();
    } catch (error) {
        appendLog(error.message);
    }
});

$("script-file-list").addEventListener("click", (event) => {
    const button = event.target.closest(".script-file-item");
    if (!button) {
        return;
    }
    $("script-file").value = button.dataset.path || "";
    $("script-file-list").querySelectorAll(".script-file-item").forEach((item) => {
        item.classList.toggle("active", item === button);
    });
});

$("script-start").addEventListener("click", async () => {
    const path = $("script-path").value || $("script-file").value;
    try {
        appendLog((await api("/script/start", { method: "POST", json: { path } })).message);
        await refreshScript();
    } catch (error) {
        appendLog(error.message);
    }
});

$("script-stop").addEventListener("click", async () => {
    try {
        appendLog((await api("/script/stop", { method: "POST" })).message);
        await refreshScript();
    } catch (error) {
        appendLog(error.message);
    }
});

$("script-bt-preset").addEventListener("change", () => {
    applyScriptBtPreset($("script-bt-preset").value);
});

if ($("script-bt-as-preset")) {
    $("script-bt-as-preset").addEventListener("change", () => {
        applyScriptBtPreset($("script-bt-as-preset").value);
    });
}

if ($("script-bt-engine")) {
    $("script-bt-engine").addEventListener("change", async () => {
        state.scriptBtPresetKey = "";
        syncScriptBtEngineUi();
        try {
            await refreshScriptBacktest();
        } catch (error) {
            appendLog(error.message);
        }
    });
}

if ($("script-bt-kind")) {
    $("script-bt-kind").addEventListener("change", async () => {
        state.scriptBtPresetKey = "";
        try {
            await refreshScriptBacktest();
        } catch (error) {
            appendLog(error.message);
        }
    });
}

if ($("script-bt-interval")) {
    $("script-bt-interval").addEventListener("change", async () => {
        state.scriptBtPresetKey = "";
        try {
            await refreshScriptBacktest();
        } catch (error) {
            appendLog(error.message);
        }
    });
}

$("script-bt-pick").addEventListener("change", () => {
    if (state.scriptBacktest) {
        renderScriptBacktest(state.scriptBacktest);
    }
});

$("script-bt-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-name]");
    if (!row || !state.scriptBacktest) {
        return;
    }
    $("script-bt-pick").value = row.dataset.name;
    const picked = selectedScriptBtResult(state.scriptBacktest);
    applyScriptBtRow(picked);
    renderScriptBacktest(state.scriptBacktest);
});

function markScriptBtBusy(message) {
    if ($("script-bt-status")) {
        $("script-bt-status").textContent = message;
    }
    if ($("script-bt-start")) {
        $("script-bt-start").disabled = true;
    }
    if ($("script-bt-cache")) {
        $("script-bt-cache").disabled = true;
    }
    if ($("script-bt-opt-start")) {
        $("script-bt-opt-start").disabled = true;
    }
}

async function startScriptBtTask(path, payload, busyText) {
    markScriptBtBusy(busyText);
    const result = await api(path, { method: "POST", json: payload });
    appendLog(result.message);
    state.scriptBacktest = Object.assign({}, state.scriptBacktest || {}, {
        running: true,
        message: result.message,
        error: "",
    });
    scheduleScriptBtPoll();
    try {
        await refreshScriptBacktest();
    } catch (error) {
        appendLog(error.message);
    }
    scheduleScriptBtPoll();
}

$("script-bt-start").addEventListener("click", async () => {
    try {
        await startScriptBtTask("/script/backtest", scriptBtPayload(), "正在回测…");
    } catch (error) {
        appendLog(error.message);
        if ($("script-bt-status")) {
            $("script-bt-status").textContent = error.message;
        }
        if ($("script-bt-start")) {
            $("script-bt-start").disabled = false;
        }
        if ($("script-bt-cache")) {
            $("script-bt-cache").disabled = false;
        }
        if ($("script-bt-opt-start")) {
            $("script-bt-opt-start").disabled = false;
        }
    }
});

$("script-bt-cache").addEventListener("click", async () => {
    try {
        await startScriptBtTask("/script/backtest/cache", scriptBtPayload(), "正在刷新行情缓存…");
    } catch (error) {
        appendLog(error.message);
        if ($("script-bt-status")) {
            $("script-bt-status").textContent = error.message;
        }
    }
});

$("script-bt-opt-start").addEventListener("click", async () => {
    try {
        await startScriptBtTask("/script/backtest/optimize", scriptBtOptimizePayload(), "正在寻优…");
    } catch (error) {
        appendLog(error.message);
        if ($("script-bt-status")) {
            $("script-bt-status").textContent = error.message;
        }
    }
});

if (state.token) {
    afterLogin().catch((error) => {
        forceLogin(error.message || "登录已过期，请重新登录");
        fillScriptFileSelect([]);
    });
} else {
    fillScriptFileSelect([]);
}
