/* Server-backed workspace. Credentials and conversation content never enter browser storage. */
"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const paths = {
  message:'<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M8 10h8M8 14h5"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  settings:'<path d="m9 3-.6 2.4-2.1 1.2L4 6l-2 3.5 1.8 1.7v2.5L2 15.5 4 19l2.3-.6 2.1 1.2L9 22h4l.6-2.4 2.1-1.2 2.3.6 2-3.5-1.8-1.8v-2.5L20 9.5 18 6l-2.3.6-2.1-1.2L13 3Z"/><circle cx="11" cy="12.5" r="3"/>',
  arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  link:'<path d="m10 13 4-4m-7 5-2 2a4 4 0 0 0 6 6l4-4a4 4 0 0 0 0-6M17 10l2-2a4 4 0 0 0-6-6L9 6a4 4 0 0 0 0 6" transform="translate(0 -1)"/>',
  spark:'<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4M18 4h4"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  sliders:'<path d="M4 6h5m4 0h7M4 12h9m4 0h3M4 18h2m4 0h10M9 3v6m4 0v6m-7 0v6"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  code:'<path d="m8 5-6 7 6 7m8-14 6 7-6 7M14 3l-4 18"/>',
  external:'<path d="M14 3h7v7M10 14 21 3M21 14v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h6"/>',
  eye:'<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  calculator:'<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M8 6h8M8 11h1m6 0h1M8 15h1m6 0h1M8 19h1m6 0h1"/>',
  chart:'<path d="M4 3v17h17M9 15v-4m5 4V6m5 9v-7"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  copy:'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const names = {calculator:"정확한 계산", "csv-statistics":"CSV 통계", "file-reader":"파일 읽기", "file-writer":"파일 쓰기", "database-query":"데이터베이스 조회", "http-api-caller":"HTTP API 연결", "web-search-adapter":"웹 검색", "python-executor":"Python 실행", "builder-internal":"프로그램 Builder"};
const toolIcon = (p) => p.name === "calculator" ? "calculator" : p.name.includes("csv") ? "chart" : p.name.includes("file") ? "file" : p.name.includes("database") ? "database" : p.runtime === "python" ? "code" : p.name.includes("search") ? "search" : "link";
const state = {session:{authenticated:false}, page:"prompt", overview:null, programs:[], settings:null, provider:"openai", filter:"all", query:"", offset:0, total:0, responses:[], busy:false, dirty:false, libraryRequest:0};
const emptyResponse = '<div class="empty-result"><span class="empty-icon">'+icon("message")+'</span><strong>결과가 여기에 표시됩니다</strong><p>프로그램 실행 결과와 AI 답변을 함께 확인하세요.</p></div>';
function hydrateIcons(root=document){ root.querySelectorAll("[data-icon]").forEach(el => {el.innerHTML=icon(el.dataset.icon);}); }
function toast(message, error=false){ const el=document.createElement("div");el.className="toast"+(error?" error":"");el.textContent=message;$("#toast-region").append(el);setTimeout(()=>el.remove(),5000); }
async function api(path, options={}) {
  const headers = {"Accept":"application/json", ...options.headers};
  if (options.body) headers["Content-Type"]="application/json";
  if (state.session.csrf_token) headers["X-Foundry-CSRF"]=state.session.csrf_token;
  let response;
  try { response=await fetch(path, {...options, headers, credentials:"same-origin"}); }
  catch { throw new Error("서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요."); }
  let body; try{body=await response.json();}catch{throw new Error("서버 응답을 확인할 수 없습니다.");}
  if (!response.ok) {
    if (response.status===401 && path!=="/ui/login") {state.session={authenticated:false}; updateIdentity();}
    if(response.status>=500)throw new Error("서버에서 요청을 완료하지 못했습니다. 연결·모델 설정을 확인하고 다시 시도해주세요.");
    const detail = typeof body.detail === "string" ? body.detail : body.error === "policy_or_configuration_error" ? "요청을 처리하지 못했습니다. 입력과 AI 연결·모델 설정을 확인해주세요." : Array.isArray(body.detail) ? "입력한 설정의 형식과 길이를 확인해주세요." : "요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.";
    throw new Error(detail);
  }
  return body;
}
function openLogin(){ $("#login-error").textContent="";$("#login-dialog").showModal();setTimeout(()=>$("#access-key").focus(),50); }
function updateIdentity(){
  const connected=state.session.authenticated, admin=state.session.role==="admin";
  $("#account-title").textContent=connected?(admin?"관리자 워크스페이스":"사용자 워크스페이스"):"워크스페이스 연결";
  $("#account-subtitle").textContent=connected?"클릭하여 연결 해제":"접속 키로 시작하기";
  $("#mobile-account").setAttribute("aria-label",connected?"워크스페이스 연결 해제":"워크스페이스 연결");
  $("#mobile-account").title=connected?"워크스페이스 연결 해제":"워크스페이스 연결";
  $("#sidebar-status").textContent=connected?"워크스페이스 연결됨":"연결 전";
  $("#sidebar-status-dot").classList.toggle("connected",connected);
  $("#settings-fields").disabled=!admin;
  $("#save-settings").disabled=!admin;
  $("#reset-settings").disabled=!admin;
  $("#settings-access-note").hidden=admin;
  if (!connected){
    state.overview=null;state.programs=[];state.settings=null;
    $("#connection-pill").classList.remove("connected");$("#connection-pill span").textContent="연결 전";
    $("#available-count").textContent="–";$("#nav-program-count").textContent="–";
    $("#ready-programs").innerHTML='<p class="muted">연결 후 확인할 수 있습니다.</p>';
    $("#current-model").textContent="설정 필요";
    $("#setup-banner").hidden=false;$("#setup-banner strong").textContent="워크스페이스를 연결해 시작하세요";
    $("#setup-banner p").textContent="프로그램을 확인하고 프롬프트를 실행할 수 있습니다.";
    $("#setup-action").innerHTML='연결하기'+icon("arrow");
  }
}
function updateOverview(){
  const o=state.overview;if(!o)return;
  $("#available-count").textContent=o.programs.active;$("#nav-program-count").textContent=o.programs.total;
  $("#summary-total").textContent=o.programs.total;$("#summary-active").textContent=o.programs.active;$("#summary-disabled").textContent=o.programs.disabled;
  $("#program-total").textContent=`${o.programs.total}개 등록됨`;
  $("#current-model").textContent=o.connection.main_model || "설정 필요";
  const provider=o.connection.provider==="openai"?"OpenAI":"호환 API";
  $("#connection-pill span").textContent=o.connection.main_model?`${provider} · 모델 설정됨`:`${provider} 연결 설정 필요`;
  $("#connection-pill").classList.toggle("connected",!!o.connection.main_model);
  $("#setup-banner").hidden=!!o.connection.main_model;
  if(!o.connection.main_model){$("#setup-banner strong").textContent="AI를 연결하면 더 많은 요청을 처리할 수 있어요";$("#setup-banner p").textContent=`현재 ${o.programs.active}개 프로그램은 AI 연결 없이도 사용할 수 있습니다.`;$("#setup-action").innerHTML='연결 설정'+icon("arrow");}
}
function showPage(page){
  if(!["prompt","programs","settings"].includes(page))page="prompt";
  state.page=page;$$('.page').forEach(p=>p.hidden=p.id!==`page-${page}`);
  $$('.nav-item').forEach(b=>{b.classList.toggle("selected",b.dataset.page===page);if(b.dataset.page===page)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");});
  $("#page-label").textContent={prompt:"프롬프트",programs:"프로그램",settings:"연결 및 모델 설정"}[page];
  document.title=`${$("#page-label").textContent} · Agent Foundry`;
  history.replaceState(null,"",page==="prompt"?"/":`/#${page}`);
  if(page==="programs")loadPrograms();
  if(page==="settings" && state.session.role==="admin" && !state.settings)loadSettings();
}
function renderExamples(items){
  const examples=items.filter(p=>p.status==="ACTIVE"&&p.visibility==="public"&&p.examples.length).slice(0,3);
  $("#example-buttons").innerHTML=examples.length?examples.map(p=>`<button class="example-button" data-example="${esc(p.examples[0].prompt)}">${icon(toolIcon(p))}${esc(names[p.name]||p.name)} 실행하기</button>`).join(""):'<button class="example-button" data-example="0.1 + 0.2">'+icon("calculator")+'소수 계산해보기</button>';
}
async function refresh(){
  if(!state.session.authenticated)return;
  try{
    const [overview, active]=await Promise.all([api("/ui/overview"),api("/ui/programs?status=ACTIVE")]);
    state.overview=overview;updateOverview();
    const publicPrograms=active.items.filter(p=>p.visibility==="public");
    $("#ready-programs").innerHTML=publicPrograms.slice(0,3).map(p=>`<div class="ready-program"><span class="icon-tile ${p.runtime==="python"?"violet":"blue"}">${icon(toolIcon(p))}</span><div><strong>${esc(names[p.name]||p.name)}</strong><small>${p.runtime==="python"?"Python 프로그램":"기본 프로그램"}</small></div></div>`).join("")||'<p class="muted">사용 가능한 프로그램이 없습니다.</p>';
    renderExamples(active.items);
    if(state.page==="programs")await loadPrograms();
  }catch(e){toast(e.message,true);}
}
function badge(p){return p.visibility==="internal"?'<span class="badge internal">내부 기능</span>':p.status==="ACTIVE"?'<span class="badge active">사용 가능</span>':'<span class="badge disabled">설정 대기</span>';}
async function loadPrograms(){
  if(!state.session.authenticated){$("#program-grid").innerHTML='<div class="empty-library">'+icon("lock")+'<strong>워크스페이스 연결이 필요합니다</strong><p>연결하면 등록된 프로그램을 확인할 수 있습니다.</p><button class="button secondary" data-login>연결하기</button></div>';return;}
  const sequence=++state.libraryRequest;
  $("#program-grid").innerHTML='<div class="loading-panel"><span class="spinner"></span>프로그램을 가져오고 있습니다</div>';
  try{
    const params=new URLSearchParams({q:state.query,status:state.filter,offset:state.offset});
    const result=await api(`/ui/programs?${params}`);if(sequence!==state.libraryRequest)return;
    state.programs=result.items;state.total=result.total;
    $("#program-grid").innerHTML=result.items.map(p=>`<article class="program-card"><div class="program-card-top"><span class="icon-tile ${p.runtime==="python"?"violet":p.status==="ACTIVE"?"blue":"amber"}">${icon(toolIcon(p))}</span>${badge(p)}</div><h3><button class="program-name" data-program="${p.id}">${esc(names[p.name]||p.name)}</button></h3><span class="program-physical">${esc(p.name)}</span><p class="program-description">${esc(p.description)}</p><div class="program-card-footer"><span>${p.runtime==="python"?"Python":p.runtime==="http"?"API":"Built-in"} · v${esc(p.version)}</span><span>${Number(p.usage_count).toLocaleString()}회 사용 ${icon("chevron")}</span></div></article>`).join("")||'<div class="empty-library">'+icon("search")+'<strong>조건에 맞는 프로그램이 없습니다</strong><p>검색어나 필터를 변경해보세요.</p></div>';
    $("#pagination").hidden=result.total<=50;$("#previous-page").disabled=state.offset===0;$("#next-page").disabled=state.offset+50>=result.total;$("#page-info").textContent=`${Math.floor(state.offset/50)+1} / ${Math.max(1,Math.ceil(result.total/50))}`;
  }catch(e){if(sequence!==state.libraryRequest)return;$("#program-grid").innerHTML=`<div class="empty-library"><strong>목록을 가져오지 못했습니다</strong><p>${esc(e.message)}</p><button class="button secondary" id="retry-programs">다시 시도</button></div>`;}
}
function programDetail(id){
  const p=state.programs.find(p=>p.id===id);if(!p)return;
  const properties=Object.entries(p.input_schema.properties||{});
  $("#program-detail").innerHTML=`<div class="dialog-top"><span class="icon-tile blue">${icon(toolIcon(p))}</span><button class="icon-button" data-close="program-dialog" aria-label="프로그램 정보 닫기">${icon("close")}</button></div>${badge(p)}<h2>${esc(names[p.name]||p.name)}</h2><span class="program-physical">${esc(p.name)}</span><p class="detail-description">${esc(p.description)}</p><dl class="detail-meta"><div><dt>상태</dt><dd>${p.status==="ACTIVE"?"사용 가능":"설정 대기"}</dd></div><div><dt>버전</dt><dd>${esc(p.version)}</dd></div><div><dt>실행 횟수</dt><dd>${Number(p.usage_count).toLocaleString()}회</dd></div><div><dt>평균 실행 시간</dt><dd>${p.usage_count?`${Math.round(p.avg_latency_ms)} ms`:"아직 실행되지 않음"}</dd></div></dl><section class="detail-section"><h3>필요한 입력</h3>${properties.length?properties.map(([name,v])=>`<p class="field-help"><code>${esc(name)}</code> · ${esc(v.description||v.type||"값")}${(p.input_schema.required||[]).includes(name)?" · 필수":""}</p>`).join(""):'<p class="field-help">별도 입력 정보가 없습니다.</p>'}</section><section class="detail-section"><h3>요청 예시</h3>${p.visibility==="public"&&p.status==="ACTIVE"&&p.examples.length?p.examples.map(e=>`<button class="example-button" data-example="${esc(e.prompt)}">${icon("message")}${esc(e.prompt)}${icon("arrow")}</button>`).join(""):'<p class="field-help">'+(p.visibility==="internal"?"시스템 내부에서 사용하는 기능입니다.":p.visibility==="admin"?"관리자 전용 기능입니다.":"연결 설정 후 사용할 수 있습니다.")+'</p>'}</section><section class="detail-section"><details><summary>실행 정보 자세히 보기</summary><pre>${esc(JSON.stringify({runtime:p.runtime,version:p.version,git_commit:p.git_commit||null,input_schema:p.input_schema,output_schema:p.output_schema},null,2))}</pre></details></section>`;
  $("#program-dialog").showModal();
}
function fillPrompt(text){if($("#program-dialog").open)$("#program-dialog").close();showPage("prompt");$("#prompt-input").value=text;updatePromptCount();$("#prompt-input").focus();}
function updatePromptCount(){$("#prompt-count").textContent=`${$("#prompt-input").value.length.toLocaleString()} / 12,000`;}
function resultHTML(result){
  if(result&&typeof result==="object"&&!Array.isArray(result)){
    const entries=Object.entries(result),labels={result:"계산 결과",count:"개수",missing_count:"결측값",sum:"합계",mean:"평균",min:"최솟값",max:"최댓값"};
    if(entries.length<=12&&entries.every(([,v])=>v===null||["string","number","boolean"].includes(typeof v)&&String(v).length<160))return `<div class="result-values ${entries.length===1?"single":""}">${entries.map(([k,v])=>`<div class="result-value"><span>${esc(labels[k]||k)}</span><strong>${esc(v===null?"–":v)}</strong></div>`).join("")}</div>`;
  }
  return `<pre class="json-result">${esc(JSON.stringify(result,null,2))}</pre>`;
}
async function submitPrompt(event){
  event.preventDefault();if(state.busy)return;if(!state.session.authenticated){openLogin();return;}
  const prompt=$("#prompt-input").value.trim();if(!prompt)return;
  const requestOptions={allow_build:$("#allow-build").checked,explain_result:$("#explain-result").checked};
  state.busy=true;$("#send-button").disabled=true;$("#new-chat").disabled=true;$("#send-button").innerHTML='<span class="spinner"></span>처리 중';
  if(!state.responses.length)$("#results").innerHTML="";
  const id=state.responses.length,item=document.createElement("article");item.className="response-card";
  item.innerHTML=`<div class="response-top"><div><span class="icon-tile small blue">${icon("spark")}</span><strong>Agent Foundry</strong></div><span class="spinner"></span></div><p class="request-text">${esc(prompt)}</p><p class="muted">요청에 맞는 프로그램과 응답을 확인하고 있습니다.</p>`;
  $("#results").prepend(item);$("#result-count").textContent="응답을 준비하고 있습니다";
  const start=performance.now();
  try{
    const response=await api("/v1/agent",{method:"POST",body:JSON.stringify({prompt,...requestOptions})});
    state.responses.push(response);const duration=((performance.now()-start)/1000).toFixed(2),direct=response.route==="deterministic",tool=response.programs.length>0;
    item.innerHTML=`<div class="response-top"><div><span class="icon-tile small blue">${icon("spark")}</span><strong>Agent Foundry</strong><span class="route-badge ${direct?"direct":""}">${direct?"프로그램 바로 실행":tool?"프로그램 실행":"AI 답변"}</span></div><button class="icon-button" data-copy="${id}" title="결과 복사" aria-label="결과 복사">${icon("copy")}</button></div><p class="request-text">${esc(prompt)}</p>${response.result!==null?resultHTML(response.result):""}${response.answer?`<div class="response-content">${esc(response.answer)}</div>`:""}<div class="response-status">${icon("check")}<span>${duration}초${direct&&!requestOptions.explain_result?" · LLM 호출 없이 처리":""}</span>${response.evaluation_job_id?'<span class="route-badge">재사용 가능성 검토 요청됨</span>':""}</div>`;
    $("#result-count").textContent=`${state.responses.length}개 응답`;refresh();
  }catch(e){state.responses.push({error:e.message});item.innerHTML=`<div class="response-top"><strong>요청을 완료하지 못했습니다</strong></div><p class="request-text">${esc(prompt)}</p><p class="response-error">${esc(e.message)}</p><button class="text-button" data-example="${esc(prompt)}">입력 다시 확인하기${icon("arrow")}</button>`;$("#result-count").textContent="입력과 연결 상태를 확인해주세요";}
  finally{state.busy=false;$("#send-button").disabled=false;$("#new-chat").disabled=false;$("#send-button").innerHTML='요청 보내기'+icon("arrow");}
}
function setProvider(value, changeUrl=false){state.provider=value;$$("[data-provider]").forEach(b=>{b.classList.toggle("chosen",b.dataset.provider===value);b.setAttribute("aria-pressed",String(b.dataset.provider===value));});if(changeUrl){$("#base-url").value=value==="openai"?"https://api.openai.com/v1":(state.settings?.provider==="compatible"?state.settings.base_url:"");$("#base-url").placeholder="https://your-provider.example/v1";}$("#api-key-link").hidden=value!=="openai";}
function applySettings(profile){
  state.settings=profile;setProvider(profile.provider);$("#base-url").value=profile.base_url;$("#provider-key").value="";$("#provider-key").type="password";$("#clear-key").checked=false;$("#clear-key-label").hidden=!profile.has_api_key;
  $("#show-key").setAttribute("aria-label","입력 중인 API 키 표시");
  $("#provider-key").placeholder=profile.has_api_key?"키가 등록되어 있습니다. 변경할 때만 입력하세요.":"API 키 입력";
  $("#key-state").textContent=profile.has_api_key?"키 등록됨":"키 미등록";$("#key-state").classList.toggle("saved",profile.has_api_key);
  ["main","router","evaluator","builder"].forEach(r=>$("#"+r+"-model").value=profile[r+"_model"]||"");
  state.dirty=false;$("#unsaved-pill").hidden=true;$("#settings-form .form-error")?.remove();
}
async function loadSettings(){try{applySettings(await api("/ui/settings"));}catch(e){toast(e.message,true);}}
function readSettings(){return {provider:state.provider,base_url:$("#base-url").value.trim(),api_key:$("#provider-key").value||null,clear_api_key:$("#clear-key").checked,...Object.fromEntries(["main","router","evaluator","builder"].map(r=>[r+"_model",$("#"+r+"-model").value.trim()]))};}
function markDirty(){state.dirty=true;$("#unsaved-pill").hidden=false;$("#test-result").textContent="";}
async function testConnection(){
  const button=$("#test-connection"),result=$("#test-result");if(!$("#base-url").reportValidity())return;
  button.disabled=true;button.innerHTML='<span class="spinner"></span>연결 확인 중';result.textContent="";result.classList.remove("error");
  try{const data=await api("/ui/connection/test",{method:"POST",body:JSON.stringify(readSettings())});$("#model-options").innerHTML=data.models.map(m=>`<option value="${esc(m)}"></option>`).join("");result.textContent=`연결 확인됨 · ${data.models.length}개 모델 조회`;toast("모델 목록을 가져왔습니다. 작업별 모델을 선택해주세요.");}
  catch(e){result.textContent=e.message;result.classList.add("error");}
  finally{button.disabled=false;button.innerHTML=icon("link")+"연결 확인 및 모델 가져오기";}
}
async function saveSettings(event){
  event.preventDefault();$("#settings-form .form-error")?.remove();const button=$("#save-settings");button.disabled=true;button.innerHTML='<span class="spinner"></span>저장 중';
  try{const profile=await api("/ui/settings",{method:"PUT",body:JSON.stringify(readSettings())});applySettings(profile);await refresh();toast("설정을 저장했습니다. 다음 요청부터 적용됩니다.");}
  catch(e){const notice=document.createElement("p");notice.className="form-error";notice.setAttribute("role","alert");notice.textContent=e.message;$("#settings-form").prepend(notice);notice.scrollIntoView({block:"nearest",behavior:"smooth"});}
  finally{button.disabled=state.session.role!=="admin";button.innerHTML=icon("check")+"설정 저장";}
}
async function login(event){
  event.preventDefault();$("#login-submit").disabled=true;$("#login-error").textContent="";
  try{state.session=await api("/ui/login",{method:"POST",body:JSON.stringify({access_key:$("#access-key").value})});$("#access-key").value="";$("#login-dialog").close();updateIdentity();await refresh();if(state.page==="settings"&&state.session.role==="admin")await loadSettings();toast("워크스페이스에 연결했습니다.");}
  catch(e){$("#login-error").textContent=e.message;}
  finally{$("#login-submit").disabled=false;}
}
async function logout(){try{await api("/ui/logout",{method:"POST"});state.session={authenticated:false};state.responses=[];$("#results").innerHTML=emptyResponse;$("#provider-key").value="";$("#prompt-input").value="";updatePromptCount();updateIdentity();if(state.page==="programs")loadPrograms();toast("워크스페이스 연결을 해제했습니다.");}catch(e){toast(e.message,true);}}
document.addEventListener("click",async event=>{
  const button=event.target.closest("button");if(!button)return;
  if(button.dataset.page)showPage(button.dataset.page);
  if(button.hasAttribute("data-login"))openLogin();
  if(button.dataset.close)$("#"+button.dataset.close).close();
  if(button.dataset.program)programDetail(button.dataset.program);
  if(button.dataset.example)fillPrompt(button.dataset.example);
  if(button.dataset.filter){state.filter=button.dataset.filter;state.offset=0;$$("[data-filter]").forEach(b=>b.classList.toggle("active",b===button));loadPrograms();}
  if(button.dataset.provider){setProvider(button.dataset.provider,true);markDirty();}
  if(button.dataset.copy!==undefined){const r=state.responses[Number(button.dataset.copy)];try{await navigator.clipboard.writeText(r.answer||JSON.stringify(r.result,null,2));toast("결과를 복사했습니다.");}catch{toast("복사할 수 없습니다. 결과를 직접 선택해 복사해주세요.",true);}}
  if(button.id==="retry-programs")loadPrograms();
});
$("#prompt-form").addEventListener("submit",submitPrompt);$("#prompt-input").addEventListener("input",updatePromptCount);
$("#prompt-input").addEventListener("keydown",e=>{if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)&&!e.isComposing){e.preventDefault();$("#prompt-form").requestSubmit();}});
$("#new-chat").addEventListener("click",()=>{if(state.busy)return;state.responses=[];$("#results").innerHTML=emptyResponse;$("#result-count").textContent="새 요청을 기다리고 있어요";$("#prompt-input").value="";updatePromptCount();$("#prompt-input").focus();});
$("#setup-action").addEventListener("click",()=>state.session.authenticated?showPage("settings"):openLogin());
$("#account-button").addEventListener("click",()=>state.session.authenticated?logout():openLogin());
$("#mobile-account").addEventListener("click",()=>state.session.authenticated?logout():openLogin());
$("#refresh-button").addEventListener("click",()=>state.session.authenticated?refresh():openLogin());
$("#login-form").addEventListener("submit",login);$("#settings-form").addEventListener("submit",saveSettings);$("#settings-fields").addEventListener("input",markDirty);
$("#test-connection").addEventListener("click",testConnection);$("#reset-settings").addEventListener("click",()=>{if(state.settings)applySettings(state.settings);});
$("#show-key").addEventListener("click",()=>{const input=$("#provider-key");input.type=input.type==="password"?"text":"password";$("#show-key").setAttribute("aria-label",input.type==="password"?"입력 중인 API 키 표시":"입력 중인 API 키 숨기기");});
let searchTimer;$("#program-search").addEventListener("input",e=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.query=e.target.value;state.offset=0;loadPrograms();},220);});
$("#previous-page").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-50);loadPrograms();});$("#next-page").addEventListener("click",()=>{state.offset+=50;loadPrograms();});
async function boot(){
  hydrateIcons();renderExamples([]);
  const fragment=location.hash, launch=fragment.startsWith("#connect=")?fragment.slice(9):null;
  if(launch)history.replaceState(null,"","/");
  try{state.session=launch?await api("/ui/login",{method:"POST",body:JSON.stringify({launch_token:launch})}):await api("/ui/session");}
  catch(e){toast(e.message,true);}
  updateIdentity();await refresh();showPage(launch?"prompt":fragment.slice(1)||"prompt");
}
boot();
