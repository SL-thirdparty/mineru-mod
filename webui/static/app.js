/* ============ MinerU 文档解析工作台 前端逻辑 ============ */
(function(){
  "use strict";

  const $ = s => document.querySelector(s);
  const pendingFiles = [];
  let pollTimer = null;
  let cfgCache = null;
  let lastSnapshot = null;
  let pvTask = null;          // 当前预览任务
  let pvImages = [];          // 当前任务图片文件列表
  let batchTarget = "current";   // 提交目标批次："current"(当前开放) | "new"(新建) | 历史批次 id
  let batchTargetName = "";      // 目标批次显示名（历史批次时用于按钮文案）

  const ST_LABEL = {
    preparing:"准备", checking:"检查服务", submitting:"提交", queued:"排队",
    processing:"解析中", downloading:"下载结果", organizing:"整理输出",
    done:"完成", error:"失败", canceled:"已取消",
  };
  const ACTIVE_STATUS = new Set(["preparing","checking","submitting","queued","processing","downloading","organizing"]);
  // 单文件生命周期（展示层）：提交后直接排队，不展示内部"准备/检查/提交"技术阶段
  const LIFE_STEPS = [
    {id:"queued",      label:"排队"},
    {id:"processing",  label:"解析"},
    {id:"downloading", label:"下载"},
    {id:"organizing",  label:"整理"},
  ];
  const IMG_EXTS = new Set(["png","jpg","jpeg","gif","webp","bmp","tiff","tif","svg"]);
  const FMT_IDS = {
    md:"fmt_md", middle_json:"fmt_middle_json", model_output:"fmt_model_output",
    content_list:"fmt_content_list", images:"fmt_images", original_file:"fmt_original_file",
  };
  const FT_COLOR = { pdf:"#e5484d", docx:"#2563eb", xlsx:"#12a150", pptx:"#d97706", img:"#7c5cff" };

  /* ---------- toast ---------- */
  let toastTimer = null;
  function toast(msg, type){
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast " + (type||"");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(()=>{ t.hidden = true; }, 3000);
  }

  /* ---------- 工具 ---------- */
  function fmtSize(n){
    if(n==null) return "";
    if(n<1024) return n+" B";
    if(n<1048576) return (n/1024).toFixed(1)+" KB";
    return (n/1048576).toFixed(1)+" MB";
  }
  function fmtTime(sec){
    sec = Math.max(0, Math.round(sec||0));
    const m = Math.floor(sec/60), s = sec%60;
    return (m<10?"0":"")+m+":"+(s<10?"0":"")+s;
  }
  function fmtClock(ts){
    if(!ts) return "";
    const d = new Date(ts*1000);
    const p = n => (n<10?"0":"")+n;
    return p(d.getHours())+":"+p(d.getMinutes());
  }
  function ftClass(name){
    const n = name.toLowerCase();
    if(/\.pdf$/.test(n)) return "pdf";
    if(/\.(doc|docx)$/.test(n)) return "docx";
    if(/\.(xls|xlsx)$/.test(n)) return "xlsx";
    if(/\.(ppt|pptx)$/.test(n)) return "pptx";
    if(/\.(png|jpe?g|webp|bmp|tiff?|svg)$/.test(n)) return "img";
    return "pdf";
  }
  function ftText(name){
    const c = ftClass(name);
    return c==="pdf"?"PDF":c==="docx"?"DOC":c==="xlsx"?"XLS":c==="pptx"?"PPT":"IMG";
  }
  function esc(s){ const d=document.createElement("div"); d.textContent=(s==null?"":String(s)); return d.innerHTML; }

  /* ---------- 待传文件管理 ---------- */
  function addPending(files){
    let added=0;
    for(const f of files){
      if(!/\.(pdf|docx?|xlsx?|pptx?|png|jpe?g|webp|bmp|tiff?|svg)$/i.test(f.name)) continue;
      if(pendingFiles.some(p=>p.file.name===f.name && p.file.size===f.size)) continue;
      pendingFiles.push({file:f});
      added++;
    }
    renderPending(); updateSubmitState();
    if(added) toast(`已添加 ${added} 个文件，当前待传 ${pendingFiles.length} 个`);
  }
  function renderPending(){
    const box = $("#pendingList");
    box.innerHTML = "";
    const has = pendingFiles.length>0;
    $("#dzEmpty").hidden = has;
    $("#dzFill").hidden = !has;
    $("#pendingCount").textContent = pendingFiles.length;
    $("#pendingSize").textContent = has ? `共 ${pendingSizeText()}` : "";
    pendingFiles.forEach((p,i)=>{
      const el = document.createElement("div");
      el.className = "prow";
      el.innerHTML = `<span class="prow-ft" style="background:${FT_COLOR[ftClass(p.file.name)]}">${ftText(p.file.name)}</span>
        <span class="prow-nm"></span><span class="prow-sz"></span><button class="prow-x" title="移除">✕</button>`;
      el.querySelector(".prow-nm").textContent = p.file.name;
      el.querySelector(".prow-sz").textContent = fmtSize(p.file.size);
      el.querySelector(".prow-x").onclick = ()=>{ pendingFiles.splice(i,1); renderPending(); updateSubmitState(); };
      box.appendChild(el);
    });
  }
  function pendingSizeText(){
    let n = 0;
    pendingFiles.forEach(p=>{ n += (p.file.size||0); });
    return fmtSize(n);
  }
  function updateSubmitState(){
    const btn = $("#btnSubmit");
    const n = pendingFiles.length;
    btn.disabled = n===0;
    if(!n){ btn.textContent = "▶ 开始解析"; return; }
    if(batchTarget === "new"){
      btn.textContent = `＋ 新建批次并解析（${n} 个文件）`;
    }else if(batchTarget !== "current"){
      btn.textContent = `↳ 追加到「${batchTargetName}」（${n} 个文件）`;
    }else if(lastSnapshot && lastSnapshot.batch && lastSnapshot.batch_open){
      btn.textContent = `↳ 追加到当前批次（${n} 个文件）`;
    }else{
      btn.textContent = `▶ 开始解析（${n} 个文件）`;
    }
  }

  /* ---------- 拖拽 / 选择 ---------- */
  const dz = $("#dropZone");
  ["dragenter","dragover"].forEach(ev=> dz.addEventListener(ev,e=>{
    e.preventDefault(); dz.classList.add("over");
  }));
  ["dragleave","drop"].forEach(ev=> dz.addEventListener(ev,e=>{
    e.preventDefault(); dz.classList.remove("over");
  }));
  dz.addEventListener("drop", e=> addPending([...e.dataTransfer.files]));
  // 点击打开文件选择器：空态点整块，文件态点底部"继续添加"（避免行内 ✕ 误触）
  $("#dzEmpty").addEventListener("click", ()=> $("#fileInput").click());
  $("#dfAddMore").addEventListener("click", ()=> $("#fileInput").click());
  $("#fileInput").addEventListener("change", e=>{
    addPending([...e.target.files]); e.target.value="";
  });
  $("#btnClearPending").addEventListener("click", ()=>{
    pendingFiles.length=0; renderPending(); updateSubmitState();
  });

  /* ---------- 提交目标批次选择器 ---------- */
  function batchLabel(b){
    if(!b) return "—";
    return b.name || b.stamp || b.id || "—";
  }
  function renderBatchTarget(data){
    const cur = data.batch || null;
    const curId = data.batch_id || (cur && cur.id) || null;
    const open = !!data.batch_open;
    const batches = data.batches || [];
    // 批次已关闭且用户未显式选择历史批次时，自动把提交目标切到“新建批次”
    if(!open && batchTarget === "current"){
      batchTarget = "new";
      batchTargetName = "新建批次";
    }
    // 所选历史批次已不存在（文件夹被删/根目录变更）时回退到当前
    if(batchTarget !== "current" && batchTarget !== "new" && !batches.some(b=>b.id===batchTarget)){
      batchTarget = "current";
      batchTargetName = "";
    }
    // 当前批次选项
    const curOpt = document.querySelector('.bt-opt[data-batch="current"]');
    const curName = curOpt.querySelector('[data-role="cur-name"]');
    const curSub = curOpt.querySelector('[data-role="cur-sub"]');
    const curDot = curOpt.querySelector('[data-role="cur-dot"]');
    if(cur){
      const count = batches.find(b=>b.id===cur.id);
      curName.textContent = batchLabel(cur);
      curSub.textContent = (open ? "开放中" : "已关闭") + " · " + ((count && count.task_count)||0) + " 个任务";
      curDot.className = "bt-dot " + (open ? "open" : "closed");
    }else{
      curName.textContent = "当前批次";
      curSub.textContent = "首次提交将创建";
      curDot.className = "bt-dot";
    }
    // 历史批次（排除当前批次；来自扫描输出根目录，含历史会话批次）
    const hist = batches.filter(b=> b.id !== curId);
    $("#btHistGroup").hidden = hist.length===0;
    const histBox = $("#btHistList");
    histBox.innerHTML = "";
    hist.slice(0, 30).forEach(b=>{
      const el = document.createElement("button");
      el.type = "button";
      el.className = "bt-opt" + (batchTarget===b.id ? " sel" : "");
      el.dataset.batch = b.id;
      el.innerHTML = `<span class="bt-dot ${b.closed?"closed":""}"></span><span class="bt-o-name"></span><span class="bt-o-sub">${b.closed?"已关闭":"开放中"} · ${b.task_count||0} 个任务</span>`;
      el.querySelector(".bt-o-name").textContent = batchLabel(b);
      el.title = b.dir || "";
      el.onclick = ()=> selectBatchTarget(b.id, batchLabel(b));
      histBox.appendChild(el);
    });
    // 新建批次命名输入：仅目标为“新建批次”时显示
    $("#btNewNameWrap").hidden = batchTarget !== "new";
    // 下拉内选中高亮
    $("#btMenu").querySelectorAll(".bt-opt").forEach(o=>{
      o.classList.toggle("sel", batchTarget===o.dataset.batch);
    });
    // 选择器顶栏
    updateBatchTargetLabel(data);
  }
  function updateBatchTargetLabel(data){
    const dot = $("#btDot"), txt = $("#btText"), sub = $("#btSub");
    const cur = data.batch || null;
    const open = !!data.batch_open;
    if(batchTarget === "new"){
      // 收起后也实时回显已输入的新建批次名称
      const nm = ($("#btNewName").value || "").trim();
      dot.className = "bt-dot new";
      txt.textContent = nm ? `新建批次（${nm}）` : "新建批次";
      sub.textContent = nm ? `下次提交创建 时间_${nm}` : "下次提交创建新目录";
    }else if(batchTarget !== "current"){
      const b = (data.batches||[]).find(x=>x.id===batchTarget) || {};
      dot.className = "bt-dot closed";
      txt.textContent = batchLabel(b);
      sub.textContent = "已关闭 · 追加到此批次";
    }else if(cur){
      dot.className = "bt-dot " + (open ? "open" : "closed");
      txt.textContent = batchLabel(cur);
      sub.textContent = open ? "开放中 · 追加到此批次" : "已关闭 · 新提交将另建批次";
    }else{
      dot.className = "bt-dot"; txt.textContent = "当前批次"; sub.textContent = "首次提交将创建";
    }
    updateSubmitState();
  }
  function selectBatchTarget(val, name){
    batchTarget = val;
    batchTargetName = name || "";
    if(val === "new"){
      // 保持下拉展开，展示批次命名输入框并聚焦
      $("#btNewNameWrap").hidden = false;
      const menu = $("#btMenu");
      if(menu.hidden){
        menu.hidden = false;
        $("#btSelect").classList.add("open");
        $("#btSelect").setAttribute("aria-expanded", "true");
      }
      setTimeout(()=>{ try{ $("#btNewName").focus(); }catch(e){} }, 30);
      if(lastSnapshot) renderBatchTarget(lastSnapshot);
      return;
    }
    closeBatchMenu();
    if(lastSnapshot) renderBatchTarget(lastSnapshot);
  }
  function openBatchMenu(){
    const menu = $("#btMenu");
    if(!lastSnapshot) renderBatchTarget({});
    menu.hidden = false;
    $("#btSelect").classList.add("open");
    $("#btSelect").setAttribute("aria-expanded", "true");
  }
  function closeBatchMenu(){
    $("#btMenu").hidden = true;
    $("#btSelect").classList.remove("open");
    $("#btSelect").setAttribute("aria-expanded", "false");
  }
  $("#btSelect").addEventListener("click", e=>{
    if(e.target.closest(".bt-menu")) return;   // 选项点击由各自 handler 处理
    const menu = $("#btMenu");
    if(menu.hidden){ openBatchMenu(); }else{ closeBatchMenu(); }
  });
  $('.bt-opt[data-batch="current"]').addEventListener("click", ()=> selectBatchTarget("current", ""));
  $('.bt-opt[data-batch="new"]').addEventListener("click", ()=> selectBatchTarget("new", "新建批次"));
  // 新建批次命名输入时，选择器收起后实时回显名称
  $("#btNewName").addEventListener("input", ()=>{
    if(batchTarget === "new" && lastSnapshot) updateBatchTargetLabel(lastSnapshot);
  });
  // 新建批次：✓ 确认并收起下拉；✕ 清除名称（均保留"新建批次"目标，名称不回退）
  $("#btNewNameOk").addEventListener("click", ()=>{
    closeBatchMenu();
    try{ $("#btNewName").blur(); }catch(e){}
    if(lastSnapshot) updateBatchTargetLabel(lastSnapshot);
  });
  $("#btNewNameClear").addEventListener("click", ()=>{
    $("#btNewName").value = "";
    if(lastSnapshot) updateBatchTargetLabel(lastSnapshot);
    try{ $("#btNewName").focus(); }catch(e){}
  });
  $("#btNewName").addEventListener("keydown", e=>{
    if(e.key==="Enter"){
      e.preventDefault(); e.stopPropagation();
      closeBatchMenu();
      try{ $("#btNewName").blur(); }catch(e){}
    }else if(e.key==="Escape"){
      e.preventDefault(); e.stopPropagation();
      closeBatchMenu();
    }
  });
  document.addEventListener("click", e=>{
    if(!e.target.closest(".bt-select")) closeBatchMenu();
  });
  $("#btSelect").addEventListener("keydown", e=>{
    const menu = $("#btMenu");
    if(e.key==="Enter" || e.key===" "){
      e.preventDefault();
      if(menu.hidden){ openBatchMenu(); }else{ closeBatchMenu(); }
    }else if(e.key==="Escape"){
      closeBatchMenu();
    }else if((e.key==="ArrowDown" || e.key==="ArrowUp") && !menu.hidden){
      e.preventDefault();
      const opts = [...menu.querySelectorAll(".bt-opt")];
      const idx = opts.findIndex(o=>o.classList.contains("sel"));
      const step = e.key==="ArrowDown" ? 1 : -1;
      const nxt = opts[Math.max(0, Math.min(opts.length-1, (idx<0?0:idx)+step))];
      if(nxt) nxt.focus();
    }
  });

  /* ---------- 提交任务 ---------- */
  $("#btnSubmit").addEventListener("click", async ()=>{
    if(!pendingFiles.length) return;
    const fd = new FormData();
    pendingFiles.forEach(p=> fd.append("files", p.file, p.file.name));
    fd.append("lang", $("#optLang").value);
    fd.append("backend", $("#optBackend").value);
    fd.append("effort", $("#optEffort").value);
    fd.append("max_pages", $("#optMaxPages").value || "1000");
    fd.append("formula", $("#optFormula").checked);
    fd.append("table", $("#optTable").checked);
    fd.append("image_analysis", $("#optImage").checked);
    fd.append("is_ocr", $("#optOcr").checked);
    fd.append("batch", batchTarget);   // "current" | "new" | 历史批次 id
    // 新建批次时可选名称：命名后文件夹为「时间_名称」
    if(batchTarget === "new"){
      fd.append("batch_name", ($("#btNewName").value || "").trim());
    }
    const btn = $("#btnSubmit");
    btn.disabled = true; btn.textContent = "提交中…";
    $("#submitMsg").textContent = "";
    try{
      const r = await fetch("/api/tasks", {method:"POST", body:fd});
      const d = await r.json();
      if(!r.ok) throw new Error((d.detail||("提交失败 "+r.status)));
      // 提交后：所选批次已成为当前批次，目标回归"当前批次"以便继续追加
      batchTarget = "current"; batchTargetName = "";
      $("#btNewName").value = "";   // 清空新建批次名称，避免下次提交误沿用
      pendingFiles.length=0; renderPending(); updateSubmitState();
      $("#submitMsg").textContent = `已加入队列 ${d.tasks.length} 个任务`;
      toast(`已提交 ${d.tasks.length} 个解析任务`, "ok");
      refresh();
    }catch(err){
      toast(err.message, "err");
    }finally{
      updateSubmitState();
    }
  });

  /* ---------- 任务渲染 ---------- */
  function render(data){
    lastSnapshot = data;
    // 引擎状态（顶栏 + 侧栏）：stopped 未启动 / starting 启动中 / running 运行中 / idle 空闲中
    const engState = data.engine_state || (data.engine_running ? "idle" : "stopped");
    const dot = $("#engineDot");
    const top = {
      running: ["run", "引擎运行中"],
      idle: ["idle", "引擎空闲中"],
      starting: ["starting", "引擎启动中…"],
      stopped: ["stopped", "引擎未启动"],
    }[engState] || ["stopped", "引擎未启动"];
    dot.className = "dot " + top[0];
    $("#engineText").textContent = top[1];
    const sideText = {running:"运行中", idle:"空闲中", starting:"启动中…", stopped:"未启动"};
    const eng = $("#sideEngine");
    eng.textContent = sideText[engState] || "未启动";
    eng.className = "sc-value " + engState;

    // 引擎当前处理阶段（EngineStageTracker 解析引擎日志得出，如"表格识别"）
    const es = $("#engineStage");
    const stage = data.engine_stage || "";
    if(stage && (engState==="running" || engState==="idle")){
      es.textContent = "当前阶段：" + stage;
      es.hidden = false;
    }else{
      es.textContent = "";
      es.hidden = true;
    }

    // 批次信息
    const batch = data.batch;
    const bOpen = data.batch_open;
    const bDir = batch ? batch.dir : "";
    const bLabel = batch ? batchLabel(batch) : "—";
    $("#topBatch").textContent = bLabel;
    $("#sideBatch").textContent = bLabel;
    $("#batchPath").textContent = bDir ? (bDir + "\\") : "—";
    $("#btnRenameBatch").hidden = !batch;   // 存在批次（开放或已关闭）即可命名/重命名
    const tag = $("#batchTag");
    if(!batch){
      $("#batchHint").textContent = "添加任务后自动创建";
      tag.hidden = true;
    }else if(bOpen){
      $("#batchHint").textContent = "追加的文件仍归入此批 · 每个文件单独建子目录";
      tag.hidden = false; tag.textContent = "批次开放中"; tag.className = "bb-tag open";
    }else{
      $("#batchHint").textContent = "批次已关闭 · 新任务将新建批次";
      tag.hidden = false; tag.textContent = "批次已关闭"; tag.className = "bb-tag closed";
    }
    renderBatchTarget(data);   // 批次选择器（当前/历史/新建）随快照刷新
    $("#btnOpenBatchDir").onclick = ()=> openDir(bDir || "");

    // 输出根目录不可写时的回退警告
    const fbWarn = data.fallback_warning || "";
    let fbEl = $("#batchFallback");
    if(fbWarn){
      if(!fbEl){
        fbEl = document.createElement("div");
        fbEl.id = "batchFallback";
        fbEl.className = "bb-warn";
        $("#batchBar").appendChild(fbEl);
      }
      fbEl.textContent = fbWarn;
    }else if(fbEl){
      fbEl.remove();
    }

    // 汇总
    const jobs = data.tasks;
    const total = data.total || 0;
    const done = data.completed || 0;
    const fail = jobs.filter(t=>t.status==="error").length;
    const proc = jobs.filter(t=>["processing","downloading","organizing"].includes(t.status)).length;
    const waiting = Math.max(0, jobs.length - done - fail - proc);
    $("#ovCount").textContent = `${done} / ${total} 份`;
    $("#ovBarInner").style.width = total>0 ? (done/total*100)+"%" : "0%";
    $("#chipProcN").textContent = proc;
    $("#chipWaitingN").textContent = waiting;
    $("#chipDoneN").textContent = done;
    $("#chipErrN").textContent = fail;
    $("#queueState").textContent = jobs.length ? `共 ${jobs.length} 个任务 · 正在处理第 ${Math.min(done+proc, total)} 份` : "暂无任务";
    $("#sideQueue").textContent = `${Math.min(done+proc, total)} / ${total} 份`;
    const navBadge = $("#navQueueBadge");
    navBadge.hidden = !(fail>0);
    navBadge.textContent = fail;

    // 清空已完成按钮：存在已完成/失败/已取消任务时可见
    const finishedCount = jobs.filter(j=>["done","error","canceled"].includes(j.status)).length;
    const clearBtn = $("#btnClearFinished");
    clearBtn.hidden = finishedCount===0;
    clearBtn.onclick = confirmClearFinished;

    // 批次关闭倒计时
    let cdText = "";
    if(bOpen && data.idle_since && (data.idle_since||0) > 0){
      const closeSec = (cfgCache && cfgCache.batch_close_seconds) || 60;
      const remain = closeSec - (Date.now()/1000 - data.idle_since);
      if(remain > 0){
        cdText = "批次空闲关闭倒计时 " + fmtTime(remain);
      }else{
        cdText = "批次已关闭";
      }
    }else{
      cdText = bOpen ? "队列处理中" : "无进行中任务";
    }
    $("#batchCountdown").textContent = cdText.startsWith("批次") ? cdText : "";

    // 任务列表
    const list = $("#taskList");
    list.innerHTML = "";
    $("#taskEmpty").hidden = jobs.length>0;

    const actives = jobs.filter(t=>ACTIVE_STATUS.has(t.status));
    jobs.forEach(t=>{
      // 展示层归一化：内部"准备/检查/提交"瞬时阶段统一视为"排队"
      const eff = (t.status==="preparing"||t.status==="checking"||t.status==="submitting") ? "queued" : t.status;
      const isProc = ["processing","downloading","organizing"].includes(eff);
      const state = isProc ? "proc" : eff==="done" ? "done" : eff==="error" ? "err" : eff==="queued" ? "queued" : "";
      const stageHtml = buildStageBar(t, eff, isProc, stage);
      const card = document.createElement("div");
      card.className = "task " + state;

      // 状态标签
      const stTag = statusTag(t, state, isProc, actives, done, total);

      // 进度区：处理中显示 第 a/N 份；排队时若引擎未就绪提示等待引擎；结束显示耗时
      let prog = "";
      if(isProc){
        const idx = actives.indexOf(t) + 1;
        const pos = done + idx;
        const pct = total>0 ? Math.round(pos/total*100) : 0;
        prog = `<div class="progline"><div class="pbar"><span style="width:${Math.max(8,pct)}%"></span></div><span class="ptxt">第 ${pos} / ${total} 份</span></div>`;
      }else if(eff==="queued" && (engState==="starting"||engState==="stopped")){
        const hint = engState==="starting" ? "等待引擎启动（首次约需 1–2 分钟）…" : "正在准备启动引擎…";
        prog = `<div class="progline"><div class="pbar start"><span style="width:0%"></span></div><span class="ptxt" style="color:var(--warn)">${hint}</span></div>`;
      }else if(eff==="queued"){
        prog = `<div class="progline"><div class="pbar"><span style="width:0%"></span></div><span class="ptxt">${t.queued_ahead!=null ? "前面还有 "+t.queued_ahead+" 个" : "排队中"}</span></div>`;
      }else if(t.status==="done"){
        prog = `<div class="progline"><div class="pbar done"><span style="width:100%"></span></div><span class="ptxt" style="color:var(--ok)">完成</span></div>`;
      }

      // 元信息
      let meta = `<span>提交 ${fmtClock(t.created_at)}</span>`;
      if(t.result && t.result.files) meta += `<span class="dotsep">·</span><span>${t.result.files.length} 个文件</span>`;
      if(isProc) meta += `<span class="dotsep">·</span><span>已用 ${fmtTime(t.elapsed)}</span>`;
      if(t.status==="done") meta += `<span class="dotsep">·</span><span>耗时 ${fmtTime(t.elapsed)}</span>`;

      let errMsg = "";
      if(t.status==="error" && t.error) errMsg = `<div class="t-err" title="${esc(t.error)}">${esc(t.error)}</div>`;
      let outPath = "";
      if(t.result && t.result.dir) outPath = `<div class="t-out" title="${esc(t.result.dir)}">→ ${esc(t.result.dir)}</div>`;

      // 右侧时间 + 操作
      let telapsed = "";
      if(isProc || t.status==="done" || t.status==="error") telapsed = `<span class="telapsed">${t.status==="done"?"耗时":(t.status==="error"?"耗时":"已用")} ${fmtTime(t.elapsed)}</span>`;

      card.innerHTML = `
        <div class="ficon ${ftClass(t.filename)}">${ftText(t.filename)}</div>
        <div class="tmid">
          <div class="tname" title="${esc(t.filename)}">${esc(t.filename)}</div>
          <div class="tmeta">${meta}</div>
          ${prog}
          ${stageHtml}
          ${outPath}
          ${errMsg}
        </div>
        <div class="tright">
          ${stTag}
          ${telapsed}
          <div class="tbtns" data-acts></div>
        </div>`;

      const acts = card.querySelector("[data-acts]");
      if(t.result){
        const prev = document.createElement("button");
        prev.className="mini-btn primary"; prev.textContent="预览";
        prev.onclick = ()=> openPreview(t);
        acts.appendChild(prev);
        if(t.result.dir){
          const op = document.createElement("button");
          op.className="mini-btn"; op.textContent="打开目录";
          op.onclick = ()=> openDir(t.result.dir);
          acts.appendChild(op);
        }
      }
      if(t.status==="error"){
        const rt = document.createElement("button");
        rt.className="mini-btn"; rt.textContent="重试";
        rt.onclick = async ()=>{
          try{
            const r = await fetch("/api/tasks/"+t.id+"/retry", {method:"POST"});
            const d = await r.json();
            if(!r.ok) throw new Error(d.detail || "重试失败");
            toast("已重新加入队列", "ok"); refresh();
          }catch(e){ toast(e.message, "err"); }
        };
        acts.appendChild(rt);
      }
      if(["done","error","canceled"].includes(t.status)){
        const del = document.createElement("button");
        del.className="mini-btn danger"; del.textContent = t.status==="done" ? "删除" : "移除";
        del.onclick = ()=> confirmDeleteTask(t);
        acts.appendChild(del);
      }
      if(["queued","checking","preparing","submitting"].includes(t.status)){
        const rm = document.createElement("button");
        rm.className="mini-btn danger"; rm.textContent="移除";
        rm.onclick = async ()=>{
          try{
            const r = await fetch("/api/tasks/"+t.id,{method:"DELETE"});
            const d = await r.json();
            if(!r.ok) throw new Error(d.detail || "移除失败");
            toast("已移除任务", "ok"); refresh();
          }catch(e){ toast(e.message, "err"); }
        };
        acts.appendChild(rm);
      }
      list.appendChild(card);
    });
  }

  function statusTag(t, state, isProc, actives, done, total){
    if(t.status==="error") return `<span class="status-tag st-err"><span class="d"></span>失败</span>`;
    if(t.status==="done")  return `<span class="status-tag st-done"><span class="d"></span>完成</span>`;
    // 排队（含内部准备/检查/提交瞬时阶段）统一显示"排队"
    if(["queued","preparing","checking","submitting"].includes(t.status))
      return `<span class="status-tag st-queued"><span class="d"></span>排队</span>`;
    if(isProc){
      const idx = actives.indexOf(t) + 1;
      const pos = done + idx;
      return `<span class="status-tag st-run"><span class="d"></span>解析中 · ${pos}/${total}</span>`;
    }
    return `<span class="status-tag st-run"><span class="d"></span>${ST_LABEL[t.status]||t.status}</span>`;
  }

  /* ---------- 单文件阶段指示条（4 步生命周期：排队→解析→下载→整理） ---------- */
  function buildStageBar(t, eff, isProc, engineStage){
    const cur = LIFE_STEPS.findIndex(s=>s.id===eff);
    let items = "";
    if(eff==="done"){
      items = LIFE_STEPS.map(s=>`<span class="st-item done"><i></i>${s.label}</span>`).join("");
    }else if(cur < 0){
      return "";
    }else{
      items = LIFE_STEPS.map((s,i)=>{
        let cls = "st-item";
        if(i < cur) cls += " done";
        else if(i === cur) cls += " cur";
        else cls += " todo";
        return `<span class="${cls}"><i></i>${s.label}</span>`;
      }).join("");
    }
    const engLine = (isProc && engineStage)
      ? `<div class="stage-eng">引擎阶段：<b>${esc(engineStage)}</b></div>` : "";
    return `<div class="stagebar">${items}</div>${engLine}`;
  }

  /* ---------- 通用确认弹窗 ---------- */
  let cfOnOk = null;
  function confirmDialog(opts){
    $("#cfTitle").textContent = opts.title || "确认操作";
    $("#cfMsg").textContent = opts.message || "";
    const extra = $("#cfExtraWrap");
    if(opts.extraLabel){
      $("#cfExtraLabel").textContent = opts.extraLabel;
      $("#cfExtra").checked = false;
      extra.hidden = false;
    }else{
      extra.hidden = true;
    }
    cfOnOk = opts.onOk || null;
    $("#confirmModal").hidden = false;
  }
  function closeConfirm(){
    $("#confirmModal").hidden = true;
    cfOnOk = null;
  }
  function confirmDeleteTask(t){
    confirmDialog({
      title:"删除任务",
      message:`确定删除「${t.filename}」吗？删除后该任务将从队列消失，且不可恢复。`,
      extraLabel:"同时删除磁盘上的输出文件",
      onOk: async (delFiles)=>{
        try{
          const q = delFiles ? "?delete_files=1" : "";
          const r = await fetch("/api/tasks/"+t.id+q, {method:"DELETE"});
          const d = await r.json();
          if(!r.ok) throw new Error(d.detail || "删除失败");
          toast(delFiles ? "任务与输出文件已删除" : "任务已删除", "ok");
          refresh();
        }catch(e){ toast(e.message, "err"); }
      }
    });
  }
  function confirmClearFinished(){
    confirmDialog({
      title:"清空已完成任务",
      message:"将删除所有已完成 / 失败 / 已取消的任务记录。",
      extraLabel:"同时删除这些任务的磁盘输出文件",
      onOk: async (delFiles)=>{
        try{
          const q = delFiles ? "?delete_files=1" : "";
          const r = await fetch("/api/tasks/clear"+q, {method:"POST"});
          const d = await r.json();
          if(!r.ok) throw new Error(d.detail || "清空失败");
          toast(`已清空 ${d.cleared} 个任务`, "ok");
          refresh();
        }catch(e){ toast(e.message, "err"); }
      }
    });
  }

  /* ---------- 批次命名 / 重命名 ---------- */
  function openRenameModal(){
    const cur = lastSnapshot && lastSnapshot.batch;
    if(!cur){ toast("当前没有可命名的批次", "err"); return; }
    $("#rnTitle").textContent = cur.name ? "重命名批次" : "命名批次";
    $("#rnInput").value = cur.name || "";
    $("#renameModal").hidden = false;
    setTimeout(()=>{ try{ $("#rnInput").focus(); $("#rnInput").select(); }catch(e){} }, 30);
  }
  function closeRenameModal(){
    $("#renameModal").hidden = true;
  }
  async function submitRename(){
    const cur = lastSnapshot && lastSnapshot.batch;
    if(!cur) return;
    const name = $("#rnInput").value.trim();
    const btn = $("#rnOk");
    btn.disabled = true;
    try{
      const r = await fetch("/api/batches/rename", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({id: cur.id, name: name}),
      });
      const d = await r.json();
      if(!r.ok) throw new Error(d.detail || "重命名失败");
      closeRenameModal();
      toast(name ? `批次已命名「${name}」` : "批次已恢复为纯时间命名", "ok");
      refresh();
    }catch(err){
      toast(err.message, "err");
    }finally{
      btn.disabled = false;
    }
  }

  /* ---------- 右侧面板拖拽调整宽度 ---------- */
  function initResizer(){
    const gutter = $("#gutterResize");
    const drawer = $("#drawer");
    // dragging：用户是否正在拖拽（与 rAF 无关，pointermove 同步应用宽度，任何环境均可靠）
    let dragging = false;

    function applyWidth(w){
      const minW = 320, maxW = Math.max(minW, Math.floor(window.innerWidth * 0.85));
      drawer.style.width = Math.max(minW, Math.min(w, maxW)) + "px";
    }
    const stop = ()=>{
      if(!dragging) return;       // 防重复清理（pointerup 与 lostpointercapture 都会触发）
      dragging = false;
      gutter.classList.remove("active");
      drawer.classList.remove("resizing");
      try{ localStorage.setItem("drawerWidth", drawer.style.width || ""); }catch(e){}
      try{ gutter.releasePointerCapture(gutter.lastPointerId); }catch(e){}
    };

    gutter.addEventListener("pointerdown", e=>{
      dragging = true;            // 立即进入拖拽态，后续 pointermove 不再被拦截
      gutter.lastPointerId = e.pointerId;
      gutter.classList.add("active");
      drawer.classList.add("resizing");
      try{ gutter.setPointerCapture(e.pointerId); }catch(e){}
      e.preventDefault();
    });
    gutter.addEventListener("pointermove", e=>{
      if(!dragging) return;
      applyWidth(window.innerWidth - e.clientX);
    });
    gutter.addEventListener("pointerup", stop);
    gutter.addEventListener("pointercancel", stop);
    gutter.addEventListener("lostpointercapture", stop);
    gutter.addEventListener("dblclick", ()=>{
      drawer.style.width = "";
      try{ localStorage.removeItem("drawerWidth"); }catch(e){}
    });
    // 键盘无障碍：聚焦后可用 ←/→ 微调宽度（每步 40px）
    gutter.addEventListener("keydown", e=>{
      if(e.key!=="ArrowLeft" && e.key!=="ArrowRight") return;
      e.preventDefault();
      const cur = parseInt(drawer.style.width, 10) || 384;
      applyWidth(cur + (e.key==="ArrowLeft" ? -40 : 40));
      try{ localStorage.setItem("drawerWidth", drawer.style.width || ""); }catch(e){}
    });
    // 恢复上次宽度
    try{
      const saved = localStorage.getItem("drawerWidth");
      if(saved) drawer.style.width = saved;
    }catch(e){}
  }

  /* ---------- 数据接口 ---------- */
  async function refresh(){
    try{
      const r = await fetch("/api/tasks");
      if(!r.ok) throw new Error("拉取失败");
      render(await r.json());
    }catch(e){ /* 服务可能刚启动 */ }
  }

  /* ---------- 目录操作 ---------- */
  async function openDir(path){
    try{
      const r = await fetch("/api/open_dir", {
        method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path: path||""})
      });
      const d = await r.json();
      if(!r.ok) throw new Error(d.detail || "打开失败");
    }catch(e){ toast(e.message, "err"); }
  }

  /* ---------- 抽屉 ---------- */
  function openDrawer(){
    $("#drawer").classList.remove("hidden");
    $("#gutterResize").classList.remove("hidden");
  }
  function closeDrawer(){
    $("#drawer").classList.add("hidden");
    $("#gutterResize").classList.add("hidden");
  }
  function setTab(t){
    const p = t==="preview";
    $("#dbodyPreview").classList.toggle("hidden", !p);
    $("#dbodySettings").classList.toggle("hidden", p);
    $("#tabPreview").classList.toggle("on", p);
    $("#tabSettings").classList.toggle("on", !p);
  }

  /* ---------- 预览 ---------- */
  async function openPreview(t){
    pvTask = t;
    openDrawer();
    setTab("preview");
    $("#pvEmpty").hidden = true;
    const content = $("#pvContent");
    content.hidden = false;
    $("#pvTitle").textContent = t.filename;
    $("#pvOpenDir").onclick = ()=> t.result && openDir(t.result.dir);
    switchView(document.querySelector('.pv-tabs button[data-view="md"]'));
    // Markdown
    let mdText = "";
    try{
      const r = await fetch("/api/tasks/"+t.id+"/result.md");
      if(r.ok) mdText = await r.text();
      else if(t.result && t.result.preview_md) mdText = t.result.preview_md;
    }catch(e){ /* ignore */ }
    $("#pvMd").innerHTML = mdText ? mdToHtml(mdText) : "<p style='color:var(--ink-3)'>（无 Markdown 内容，可能未勾选该导出项）</p>";
    // JSON
    try{
      const r = await fetch("/api/tasks/"+t.id+"/result.json");
      if(r.ok){
        const j = await r.json();
        $("#pvJson").textContent = JSON.stringify(j, null, 2);
      }else{
        $("#pvJson").textContent = "（无 JSON 内容）";
      }
    }catch(e){ $("#pvJson").textContent = "（JSON 加载失败）"; }
    // 图片
    pvImages = [];
    try{
      const r = await fetch("/api/tasks/"+t.id+"/files");
      if(r.ok){
        const d = await r.json();
        pvImages = (d.files||[]).filter(f=>IMG_EXTS.has(f.ext));
      }
    }catch(e){ /* ignore */ }
    renderImages();
  }

  function switchView(btn){
    document.querySelectorAll(".pv-tabs button").forEach(b=>b.classList.toggle("on", b===btn));
    const v = btn.dataset.view;
    $("#pvViewMd").classList.toggle("hidden", v!=="md");
    $("#pvViewJson").classList.toggle("hidden", v!=="json");
    $("#pvViewImages").classList.toggle("hidden", v!=="images");
    if(v==="images") renderImages();
  }
  function renderImages(){
    const box = $("#pvImgs");
    box.innerHTML = "";
    if(!pvTask || !pvImages.length){
      box.innerHTML = `<div class="pv-imgs-empty">（无提取图片，可能未勾选图片导出项）</div>`;
      return;
    }
    pvImages.forEach(f=>{
      const el = document.createElement("div");
      el.className = "img";
      el.innerHTML = `<img src="/api/tasks/${pvTask.id}/file?name=${encodeURIComponent(f.rel)}" alt="${esc(f.name)}"/><div class="cap">${esc(f.name)}</div>`;
      box.appendChild(el);
    });
  }

  /* ---------- Markdown 渲染（markdown-it + 复杂表格 + 公式 + 高亮 + 安全清洗） ---------- */
  let mdEngine = null;
  function initMd(){
    if(mdEngine) return mdEngine;
    const md = window.markdownit({
      html: true,          // MinerU 复杂表格以 HTML 表格输出，需放行（DOMPurify 负责清洗）
      linkify: true,
      typographer: false,
      breaks: true,
      highlight(str, lang){
        let code;
        if(lang && window.hljs && window.hljs.getLanguage(lang)){
          try{
            code = window.hljs.highlight(str, {language: lang, ignoreIllegals: true}).value;
          }catch(e){
            code = md.utils.escapeHtml(str);
          }
        }else{
          code = md.utils.escapeHtml(str);
        }
        return '<pre class="hljs"><code' + (lang ? ' class="language-' + md.utils.escapeHtml(lang) + '"' : '') + '>' + code + '</code></pre>';
      }
    });

    // Multimarkdown 表格：支持 rowspan/colspan/表头分组/单元格内换行
    if(window.markdownitMultimdTable){
      md.use(window.markdownitMultimdTable, {multiline: true, rowspan: true, multibody: true});
    }

    // 行内公式 $...$ 与段落内显示公式 $$...$$（行首独立 $$ 由块级规则处理）
    md.inline.ruler.before("escape", "math_inline", (state, silent)=>{
      const start = state.pos;
      if(state.src.charCodeAt(start) !== 36) return false;          // $
      if(start > 0 && state.src.charCodeAt(start-1) === 92) return false;  // 转义 \$
      const display = state.src.charCodeAt(start+1) === 36;         // $$ 视为显示公式
      const openLen = display ? 2 : 1;
      let end = -1;
      for(let i = start + openLen; i < state.posMax; i++){
        if(state.src.charCodeAt(i) === 36){
          if(display){
            if(state.src.charCodeAt(i+1) === 36){ end = i; break; }  // 找到闭合 $$
          }else{
            if(state.src.charCodeAt(i+1) !== 36){ end = i; break; }  // 单个 $ 闭合
          }
        }
      }
      if(end < 0) return false;
      const content = state.src.slice(start + openLen, end).trim();
      if(!content) return false;
      if(silent) return true;
      const token = state.push("math_inline", "math", 0);
      token.content = content;
      token.display = display;
      state.pos = end + (display ? 2 : 1);
      return true;
    });
    md.renderer.rules.math_inline = (tokens, idx)=>{
      try{
        return window.katex.renderToString(tokens[idx].content, {throwOnError:false, displayMode:!!tokens[idx].display});
      }catch(e){
        return '<span class="math-raw">' + md.utils.escapeHtml(tokens[idx].content) + '</span>';
      }
    };

    // 块级公式 $$...$$（支持单行 $$x$$ 与多行 $$ ... $$）
    md.block.ruler.before("paragraph", "math_block", (state, startLine, endLine, silent)=>{
      const s0 = state.bMarks[startLine] + state.tShift[startLine];
      const e0 = state.eMarks[startLine];
      const first = state.src.slice(s0, e0).trim();
      if(!first.startsWith("$$")) return false;
      let content, nextLine;
      if(first.length > 2 && first.endsWith("$$")){          // 单行 $$x$$
        content = first.slice(2, -2).trim();
        nextLine = startLine + 1;
      }else{
        nextLine = startLine + 1;
        let found = false;
        while(nextLine < endLine){
          const s = state.bMarks[nextLine] + state.tShift[nextLine];
          const e = state.eMarks[nextLine];
          if(state.src.slice(s, e).trim() === "$$"){ found = true; break; }
          nextLine++;
        }
        if(!found) return false;
        const lines = [];
        for(let i = startLine+1; i < nextLine; i++){
          const s = state.bMarks[i] + state.tShift[i];
          const e = state.eMarks[i];
          lines.push(state.src.slice(s, e));
        }
        content = lines.join("\n");
        nextLine = nextLine + 1;
      }
      if(!content.trim()) return false;
      if(silent) return true;
      const token = state.push("math_block", "math", 0);
      token.content = content;
      token.block = true;
      state.line = nextLine;
      return true;
    });
    md.renderer.rules.math_block = (tokens, idx)=>{
      try{
        return '<div class="math-block">' + window.katex.renderToString(tokens[idx].content, {throwOnError:false, displayMode:true}) + '</div>';
      }catch(e){
        return '<div class="math-block math-raw">' + md.utils.escapeHtml(tokens[idx].content) + '</div>';
      }
    };

    mdEngine = md;
    return md;
  }

  function mdToHtml(src){
    const md = initMd();
    let html = md.render(src || "");
    // 表格外层包横向滚动容器，避免超宽表格撑破预览区；兼容带属性的 <table ...>
    html = html.replace(/<table\b/g, '<div class="markdown-table-container"><table')
               .replace(/<\/table>/g, '</table></div>');
    if(window.DOMPurify){
      html = window.DOMPurify.sanitize(html, {
        // 显式放行表格合并属性（MinerU 复杂表格依赖 rowspan/colspan 才能完整显示）
        ADD_ATTR:["target","rowspan","colspan","align","valign","width","height"],
        USE_PROFILES:{html:true, mathMl:true},
      });
    }
    return renderCellMath(html);
  }

  /* 表格单元格内的公式渲染：
     MinerU 的复杂表格以原生 HTML <table> 输出，其中的 $...$/$$...$$ 不会被
     markdown-it 的 inline 规则处理（raw HTML 原样透传），故在此对 td/th 的
     文本节点做二次公式转换，保证表格内公式也完整显示。 */
  function renderCellMath(html){
    if(!window.katex || !html || html.indexOf("$") === -1) return html;
    const holder = document.createElement("div");
    holder.innerHTML = html;
    if(!holder.querySelector) return html;
    const cells = holder.querySelectorAll("td,th");
    if(!cells.length) return html;
    let changed = false;
    cells.forEach(cell=>{
      const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while(walker.nextNode()){
        const n = walker.currentNode;
        if(n.nodeValue && n.nodeValue.indexOf("$") !== -1 &&
           !n.parentNode.closest("code,pre,.katex")) nodes.push(n);
      }
      nodes.forEach(node=>{
        const out = convertCellMath(node.nodeValue);
        if(out === null) return;
        const span = document.createElement("span");
        span.innerHTML = out;
        node.parentNode.replaceChild(span, node);
        changed = true;
      });
    });
    return changed ? holder.innerHTML : html;
  }
  function convertCellMath(text){
    if(!text || text.indexOf("$") === -1) return null;
    let out = "", i = 0, changed = false;
    const n = text.length;
    while(i < n){
      const idx = text.indexOf("$", i);
      if(idx === -1){ out += text.slice(i); break; }
      if(idx > 0 && text.charCodeAt(idx-1) === 92){ out += text.slice(i, idx+1); i = idx+1; continue; }
      const isBlock = text[idx+1] === "$";
      const contentStart = idx + (isBlock ? 2 : 1);
      let j = contentStart, end = -1;
      while(j < n){
        if(text[j] === "$"){
          if(isBlock && text[j+1] === "$"){ end = j+1; break; }
          if(!isBlock && text[j+1] === "$"){ j += 2; continue; }
          end = j; break;
        }
        j++;
      }
      if(end < 0){ out += text.slice(i); break; }
      const content = text.slice(contentStart, end).trim();
      if(!content || content.indexOf("$") !== -1){
        out += text.slice(i, end+1); i = end+1; continue;
      }
      try{
        out += window.katex.renderToString(content, {throwOnError:false, displayMode:false});
        changed = true;
      }catch(e){
        out += text.slice(i, end+1);
      }
      i = end+1;
    }
    return changed ? out : null;
  }

  /* ---------- 运行设置 ---------- */
  // 将已保存的默认解析参数应用到「文档解析」页各控件
  function applyDefaultParams(dp){
    dp = dp || {};
    if(["ch","en","ja","ko","auto"].includes(dp.lang)) $("#optLang").value = dp.lang;
    if(["pipeline","hybrid-engine"].includes(dp.backend)) $("#optBackend").value = dp.backend;
    if(["high","medium","low","none"].includes(dp.effort)) $("#optEffort").value = dp.effort;
    const mp = parseInt(dp.max_pages, 10);
    if(mp > 0) $("#optMaxPages").value = mp;
    $("#optFormula").checked = dp.formula !== false;
    $("#optTable").checked = dp.table !== false;
    $("#optImage").checked = dp.image_analysis !== false;
    $("#optOcr").checked = !!dp.is_ocr;
  }
  async function loadConfig(){
    try{
      const r = await fetch("/api/config");
      if(!r.ok) throw new Error("读取配置失败");
      cfgCache = await r.json();
      const c = cfgCache;
      $("#setOutputDir").value = c.output_dir || "";
      $("#setIdle").value = c.idle_release_seconds != null ? c.idle_release_seconds : 30;
      $("#setBatchClose").value = c.batch_close_seconds != null ? c.batch_close_seconds : 60;
      const f = c.formats || {};
      for(const k in FMT_IDS){ const el = $("#"+FMT_IDS[k]); if(el) el.checked = f[k] !== false; }
      // 解析参数默认值：设置面板回显 + 文档解析页套用
      const dp = c.default_params || {};
      $("#setParamLang").value = dp.lang || "ch";
      $("#setParamBackend").value = dp.backend || "pipeline";
      $("#setParamEffort").value = dp.effort || "medium";
      $("#setParamMaxPages").value = dp.max_pages != null ? dp.max_pages : 1000;
      $("#setParamFormula").checked = dp.formula !== false;
      $("#setParamTable").checked = dp.table !== false;
      $("#setParamImage").checked = dp.image_analysis !== false;
      $("#setParamOcr").checked = !!dp.is_ocr;
      applyDefaultParams(dp);
    }catch(e){ toast(e.message, "err"); }
  }
  async function saveSettings(){
    const payload = {
      output_dir: $("#setOutputDir").value.trim(),
      idle_release_seconds: Math.max(5, Math.min(3600, parseInt($("#setIdle").value||"30",10)||30)),
      batch_close_seconds: Math.max(10, Math.min(86400, parseInt($("#setBatchClose").value||"60",10)||60)),
      formats: {},
      default_params: {
        lang: $("#setParamLang").value,
        backend: $("#setParamBackend").value,
        effort: $("#setParamEffort").value,
        max_pages: Math.max(1, Math.min(99999, parseInt($("#setParamMaxPages").value||"1000",10)||1000)),
        formula: $("#setParamFormula").checked,
        table: $("#setParamTable").checked,
        image_analysis: $("#setParamImage").checked,
        is_ocr: $("#setParamOcr").checked,
      },
    };
    for(const k in FMT_IDS) payload.formats[k] = $("#"+FMT_IDS[k]).checked;
    const btn = $("#btnSaveSettings");
    btn.disabled = true; btn.textContent = "保存中…";
    try{
      const r = await fetch("/api/config", {
        method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload),
      });
      const d = await r.json();
      if(!r.ok) throw new Error(d.detail || ("保存失败 "+r.status));
      cfgCache = d;
      // 保存后立即把新默认值套用到文档解析页
      applyDefaultParams(d.default_params || payload.default_params);
      toast("设置已保存并生效", "ok");
      refresh();
    }catch(err){
      toast(err.message, "err");
    }finally{
      btn.disabled = false; btn.textContent = "保存设置";
    }
  }

  /* ---------- 打开设置 / 导航 ---------- */
  async function openSettings(){
    if(!cfgCache) await loadConfig();
    openDrawer(); setTab("settings");
  }
  function setNav(on){
    document.querySelectorAll(".nav-item").forEach(n=> n.classList.toggle("on", n===on));
  }

  /* ---------- 事件绑定 ---------- */
  $("#btnSettings").addEventListener("click", openSettings);
  $("#navSettings").addEventListener("click", openSettings);
  $("#navParse").addEventListener("click", ()=>{ setNav($("#navParse")); closeDrawer(); });
  $("#navQueue").addEventListener("click", ()=>{
    setNav($("#navQueue"));
    closeDrawer();
    $("#queuePanel").scrollIntoView({behavior:"smooth", block:"start"});
  });
  $("#btnOpenOutput").addEventListener("click", ()=> openDir(""));
  $("#btnCloseDrawer").addEventListener("click", closeDrawer);
  $("#tabPreview").addEventListener("click", ()=> setTab("preview"));
  $("#tabSettings").addEventListener("click", openSettings);
  document.querySelectorAll(".pv-tabs button").forEach(b=>{
    b.addEventListener("click", ()=> switchView(b));
  });
  $("#btnSaveSettings").addEventListener("click", saveSettings);
  $("#btnPickDir").addEventListener("click", async ()=>{
    try{
      const r = await fetch("/api/pick_dir", {method:"POST"});
      const d = await r.json();
      if(!r.ok) throw new Error(d.detail || "选择失败");
      if(!d.canceled && d.path){
        $("#setOutputDir").value = d.path;
        toast("已选择目录", "ok");
      }
    }catch(e){ toast(e.message, "err"); }
  });
  $("#btnOpenRoot").addEventListener("click", ()=> openDir($("#setOutputDir").value.trim()));
  // 批次命名/重命名弹窗
  $("#btnRenameBatch").addEventListener("click", openRenameModal);
  $("#rnCancel").addEventListener("click", closeRenameModal);
  $("#rnOk").addEventListener("click", submitRename);
  $("#rnInput").addEventListener("keydown", e=>{
    if(e.key==="Enter"){ e.preventDefault(); submitRename(); }
    else if(e.key==="Escape"){ e.preventDefault(); closeRenameModal(); }
  });
  $("#renameModal").addEventListener("click", e=>{
    if(e.target && e.target.classList.contains("modal-mask")) closeRenameModal();
  });
  document.addEventListener("keydown", e=>{
    if(e.key==="Escape" && !$("#renameModal").hidden){ closeRenameModal(); return; }
    if(e.key==="Escape" && !$("#confirmModal").hidden){ closeConfirm(); return; }
    if(e.key==="Escape" && !$("#drawer").classList.contains("hidden")) closeDrawer();
  });
  $("#cfCancel").addEventListener("click", closeConfirm);
  $("#cfOk").addEventListener("click", ()=>{
    const extra = !$("#cfExtraWrap").hidden && $("#cfExtra").checked;
    const ok = cfOnOk; closeConfirm();
    if(ok) ok(extra);
  });
  $("#confirmModal").addEventListener("click", e=>{
    if(e.target && e.target.classList.contains("modal-mask")) closeConfirm();
  });

  /* ---------- 启动：轮询 ---------- */
  initResizer();
  pollTimer = setInterval(refresh, 2500);
  refresh();
  loadConfig();
})();
