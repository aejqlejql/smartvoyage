'use strict';
const $ = (id) => document.getElementById(id);
const state = {user:null, csrf:'', tab:'planner', plans:[], selected:null, conversations:[], cid:null, mode:'demo', register:false, busy:false, keys:new Map()};
const money = (cents) => `¥${(cents / 100).toFixed(2)}`;
const when = (seconds) => new Date(seconds * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
const labels = {pending_payment:'待模拟支付', confirmed:'模拟支付成功', cancelled:'已取消', refunded:'已模拟退款', expired:'超时已关闭'};
function el(tag, props={}, children=[]) {
  const node=document.createElement(tag);
  for(const [key,value] of Object.entries(props)) {
    if(key==='class') node.className=value;
    else if(key==='text') node.textContent=value;
    else if(key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else node.setAttribute(key,value);
  }
  for(const child of children) node.append(typeof child==='string'?document.createTextNode(child):child);
  return node;
}
function replace(node, children) { node.replaceChildren(...children); }
let toastTimer;
function toast(message, error=false) {
  $('toast').textContent=message; $('toast').className=error?'error-toast':''; $('toast').hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,6500);
}
async function api(path, {method='GET',body,headers={}}={}) {
  const response=await fetch(path,{method,credentials:'same-origin',
    headers:{...(body!==undefined?{'Content-Type':'application/json'}:{}),...(method!=='GET'?{'X-CSRF-Token':state.csrf}:{}),...headers},
    body:body!==undefined?JSON.stringify(body):undefined});
  let result; try {result=await response.json();} catch {throw new Error('服务响应异常，请刷新后重试');}
  if(!response.ok) {
    if(response.status===401&&state.user) showAuth();
    const detail=result.errors?.map(x=>`${x.field.replace('body.','')}：${x.message}`).join('；');
    throw new Error(detail||result.message||'操作失败');
  }
  return result.data;
}
function safe(action) {return async(event)=>{try{await action(event);}catch(error){toast(error.message,true);}};}
async function confirmAction(title,text) {
  $('confirm-title').textContent=title; $('confirm-text').textContent=text;
  const dialog=$('confirm-dialog'); dialog.returnValue='cancel'; dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
}
function showAuth() {
  state.user=null;state.csrf='';state.cid=null;state.selected=null;state.plans=[];state.conversations=[];state.keys.clear();
  $('app').hidden=true;$('auth').hidden=false;
  for(const id of ['messages','trace','run-list','order-list','audit-list','plan-list','conversation-list']) $(id).replaceChildren();
  $('auth-password').value='';
}
async function enter(data) {
  state.user=data.user;state.csrf=data.csrf_token;$('auth').hidden=true;$('app').hidden=false;$('user-name').textContent=state.user.name;
  const config=await api('/api/config');state.mode=config.mode;
  $('mode-label').textContent=config.mode==='demo'?'规则演示模式':'联网 Agent 模式';
  $('chat-mode-note').textContent=config.mode==='demo'?'规则演示会引用你最近保存的方案，未调用大模型。需要规划时请使用行程表单。':'通过已配置的 LangGraph / A2A / MCP 服务回复。方案与订单仍采用独立的模拟目录，聊天不会自动下单。';
  resetForm(); emptyDetail(); await loadPlans(); state.conversations=await api('/api/conversations');state.cid=state.conversations[0]?.id||null;
  renderConversations(); await selectTab('planner');
}
$('auth-toggle').addEventListener('click',()=>{
  state.register=!state.register;$('name-label').hidden=!state.register;$('auth-name').required=state.register;
  $('auth-title').textContent=state.register?'创建你的旅行账号':'欢迎回来';$('auth-submit').textContent=state.register?'创建账号并开始':'登录';
  $('auth-toggle').textContent=state.register?'已有账号？直接登录':'还没有账号？创建一个';
  $('auth-password').autocomplete=state.register?'new-password':'current-password';$('auth-error').textContent='';
});
$('auth-form').addEventListener('submit',async(event)=>{
  event.preventDefault();$('auth-submit').disabled=true;$('auth-error').textContent='';
  try {
    const payload={email:$('auth-email').value,password:$('auth-password').value};if(state.register)payload.name=$('auth-name').value;
    const data=await api(`/api/auth/${state.register?'register':'login'}`,{method:'POST',body:payload});
    $('auth-password').value='';await enter(data);
  }catch(error){$('auth-error').textContent=error.message;}finally{$('auth-submit').disabled=false;}
});
$('logout').addEventListener('click',safe(async()=>{await api('/api/auth/logout',{method:'POST'});showAuth();}));
async function selectTab(tab) {
  state.tab=tab;
  for(const node of document.querySelectorAll('.page'))node.hidden=node.id!==tab;
  for(const node of document.querySelectorAll('[data-tab]'))node.classList.toggle('active',node.dataset.tab===tab);
  $('page-title').textContent={planner:'下一站，去哪里？',chat:'给旅行，多一点灵感。',orders:'把每一个选择，安排妥当。',activity:'每一步，都有迹可循。'}[tab];
  if(tab==='orders')await loadOrders();if(tab==='activity')await loadAudit();
  if(tab==='chat'){if(!state.cid)await newConversation();else await loadMessages();}
}
for(const node of document.querySelectorAll('[data-tab]'))node.addEventListener('click',safe(()=>selectTab(node.dataset.tab)));
function resetForm() {
  state.selected=null;$('plan-form').reset();
  $('departure').value=new Date(Date.now()+7*86400000).toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'});
  $('departure').min=new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'});
  $('plan-form-title').textContent='规划一次旅行';$('plan-submit').textContent='生成行程与报价 →';renderPlanList();
}
function emptyDetail() {
  replace($('plan-detail'),[el('div',{class:'empty-hero'},[el('p',{class:'eyebrow',text:'A NEW PLACE. A NEW STORY.'}),el('h2',{text:'开始一段新的旅行。'}),el('p',{class:'muted',text:'填写出发偏好，生成新的行程方案。'})])]);
}
$('reset-plan').addEventListener('click',()=>{resetForm();emptyDetail();});
function tripPayload() {return{origin:$('origin').value,destination:$('destination').value,departure_date:$('departure').value,days:Number($('days').value),travelers:Number($('travelers').value),budget_cents:Math.round(Number($('budget').value)*100),transport:$('transport').value,include_insurance:$('insurance').checked,notes:$('notes').value};}
$('plan-form').addEventListener('submit',safe(async(event)=>{
  event.preventDefault();$('plan-submit').disabled=true;
  try {
    const selected=state.selected;const payload=tripPayload();if(selected)payload.version=selected.version;
    const plan=await api(selected?`/api/plans/${selected.id}`:'/api/plans',{method:selected?'PUT':'POST',body:payload});
    await loadPlans();selectPlan(plan);toast(selected?'方案已更新，报价重新计算':'行程方案已保存');
  }finally{$('plan-submit').disabled=false;}
}));
async function loadPlans(){state.plans=await api('/api/plans');renderPlanList();}
function renderPlanList() {
  replace($('plan-list'),state.plans.length?state.plans.map(plan=>el('button',{
    class:`plan-card ${state.selected?.id===plan.id?'active':''}`,onclick:()=>selectPlan(plan)
  },[el('p',{class:'eyebrow',text:`${plan.request.departure_date} · ${plan.request.transport==='train'?'高铁':'飞机'}往返`}),
    el('h3',{text:plan.title}),el('div',{class:'amount',text:money(plan.quote.estimated_total_cents)}),
    el('span',{class:'muted small',text:`含本地开销预估 · v${plan.version} · ${plan.expired?'报价已过期':'点击查看与编辑'}`})]))
    :[el('p',{class:'empty-state',text:'还没有保存的行程。生成你的第一份旅行方案吧。'})]);
}
function selectPlan(plan) {
  state.selected=plan;const r=plan.request;
  for(const [id,value] of Object.entries({origin:r.origin,destination:r.destination,departure:r.departure_date,days:r.days,travelers:r.travelers,budget:r.budget_cents/100,transport:r.transport,notes:r.notes}))$(id).value=value;
  $('insurance').checked=r.include_insurance;$('plan-form-title').textContent='编辑当前方案';$('plan-submit').textContent='更新方案并重新报价 →';
  renderPlanList();renderPlanDetail(plan);
}
function costRow(label,value,cls='small') {return el('div',{class:`cost-row ${cls}`},[el('span',{text:label}),el('span',{text:value})]);}
function renderPlanDetail(plan) {
  const q=plan.quote;
  const table=el('table',{class:'quote-table'},[
    el('thead',{},[el('tr',{},[el('th',{text:'模拟预订项'}),el('th',{text:'数量'}),el('th',{text:'金额'})])]),
    el('tbody',{},q.items.map(item=>el('tr',{},[
      el('td',{},[item.label,el('small',{text:`${item.service_date} · 单价 ${money(item.unit_price_cents)}`})]),
      el('td',{text:String(item.quantity)}),el('td',{text:money(item.subtotal_cents)})])))
  ]);
  const checked=el('input',{type:'checkbox','aria-label':'确认模拟预订说明'});
  const book=el('button',{class:'primary',text:'确认方案，创建模拟订单',type:'button',onclick:safe(async()=>{
    if(!checked.checked){toast('请先勾选确认方案与模拟预订说明',true);return;}
    if(!await confirmAction('确认模拟预订',`${plan.title}\n预订金额 ${money(q.amount_cents)}\n不含本地开销预估 ${money(q.local_estimate_cents)}。\n仅创建模拟订单，不会真实出票、订房或扣款。`))return;
    book.disabled=true;
    try {
      const key=state.keys.get(`${plan.id}:${plan.version}`)||crypto.randomUUID();state.keys.set(`${plan.id}:${plan.version}`,key);
      await api('/api/orders',{method:'POST',headers:{'Idempotency-Key':key},body:{plan_id:plan.id,version:plan.version,confirmed:true}});
      toast('模拟订单已创建，请在15分钟内完成模拟支付');await selectTab('orders');
    }finally{book.disabled=false;}
  })});
  replace($('plan-detail'),[
    el('div',{class:'detail-head'},[el('div',{class:'cost-row'},[el('span',{class:'pill',text:`方案 v${plan.version} · 示例目录`}),el('a',{href:`/api/plans/${plan.id}/export`,class:'text-button',text:'下载行程 Markdown'})]),el('h2',{text:plan.title}),
      el('p',{class:'muted small',text:`${plan.request.departure_date} 出发 · ${plan.request.days-1}晚住宿 · 报价有效至 ${when(plan.expires_at)}（北京时间）`})]),
    el('div',{class:'quote-body'},[el('p',{class:'eyebrow',text:'02 / YOUR ITINERARY'}),
      ...q.itinerary.map(day=>el('div',{class:'day-row'},[
        el('div',{class:'day-num',text:`DAY ${String(day.day).padStart(2,'0')}`}),
        el('div',{},[el('h3',{text:day.title}),el('p',{text:day.activity}),el('p',{class:'muted small',text:day.date}),el('p',{class:'muted small',text:day.note})])])),
      ...(plan.request.notes?[el('p',{class:'muted small',text:`个人备注：${plan.request.notes}`})]:[]),table,
      el('div',{class:'cost-summary'},[
        costRow('模拟预订金额',money(q.amount_cents),'total'),
        costRow('餐饮及市内交通预估（不支付）',money(q.local_estimate_cents),'small muted'),
        costRow('旅行合计预估 / 总预算',`${money(q.estimated_total_cents)} / ${money(q.budget_cents)}`)]),
      el('ul',{class:'quote-warnings'},q.warnings.map(text=>el('li',{text}))),
      el('label',{class:'checkbox'},[checked,'我已核对方案与金额，知悉所有交易均为模拟']),book])]);
  if(plan.expires_at<=Date.now()/1000){book.disabled=true;book.textContent='报价已过期，请更新方案后再预订';}
}
function renderConversations() {
  replace($('conversation-list'),state.conversations.map(c=>el('button',{
    class:`conversation-item ${c.id===state.cid?'active':''}`,text:c.title,
    onclick:safe(async()=>{state.cid=c.id;renderConversations();await loadMessages();})})));
  $('conversation-title').textContent=state.conversations.find(c=>c.id===state.cid)?.title||'旅行助手';
}
async function newConversation() {
  const c=await api('/api/conversations',{method:'POST',body:{title:`旅行咨询 ${state.conversations.length+1}`}});
  state.conversations.unshift(c);state.cid=c.id;renderConversations();await loadMessages();
}
$('new-conversation').addEventListener('click',safe(newConversation));
async function loadMessages() {
  const cid=state.cid;const messages=await api(`/api/conversations/${cid}/messages`);if(cid!==state.cid)return;
  replace($('messages'),messages.length?messages.map(m=>el('div',{class:`message ${m.role}`,text:m.content}))
    :[el('p',{class:'empty-state',text:'聊聊你的旅行想法。每个会话独立保存，只有你可以查看。'})]);
  $('messages').scrollTop=$('messages').scrollHeight;await loadRuns();
}
async function loadRuns() {
  const cid=state.cid;const runs=await api(`/api/runs?conversation_id=${encodeURIComponent(cid)}`);if(cid!==state.cid)return;
  $('trace').textContent='';replace($('run-list'),runs.map(run=>el('button',{
    class:'secondary',text:`${run.mode} · ${run.status} · ${run.duration_ms}ms`,
    onclick:safe(async()=>{const data=await api(`/api/runs/${run.id}`);if(cid===state.cid)$('trace').textContent=JSON.stringify(data.trace,null,2);})})));
}
$('chat-form').addEventListener('submit',safe(async(event)=>{
  event.preventDefault();if(state.busy)return;const message=$('chat-input').value.trim();if(!message)return;
  state.busy=true;$('chat-submit').disabled=true;$('chat-submit').textContent='处理中…';const cid=state.cid;
  try{await api('/api/chat',{method:'POST',body:{conversation_id:cid,message}});$('chat-input').value='';}
  finally{state.busy=false;$('chat-submit').disabled=false;$('chat-submit').textContent='发送';if(cid===state.cid)await loadMessages();}
}));
$('clear-chat').addEventListener('click',safe(async()=>{
  if(!state.cid)return;
  if(!await confirmAction('清空当前会话？','只清空当前会话的聊天与偏好。历史执行记录仍保留在活动与运行记录中。'))return;
  await api('/api/memory/clear',{method:'POST',body:{conversation_id:state.cid}});await loadMessages();toast('当前会话已清空');
}));
async function loadOrders() {
  const orders=await api('/api/orders');
  replace($('order-list'),orders.length?orders.map(order=>{
    const q=order.quote;const actions=[];
    if(order.status==='pending_payment')actions.push(el('button',{class:'primary',text:`模拟支付 ${money(order.amount_cents)}`,onclick:safe(()=>orderAction(order,'pay'))}));
    if(['pending_payment','confirmed'].includes(order.status))actions.push(el('button',{class:'secondary',text:order.status==='confirmed'?'取消并模拟退款':'取消订单',onclick:safe(()=>orderAction(order,'cancel'))}));
    return el('article',{class:'order-card'},[
      el('div',{class:'order-summary'},[
        el('div',{},[el('span',{class:`status ${order.status}`,text:labels[order.status]||order.status}),
          el('h3',{text:q.items[0].label.split(' · ')[0]}),el('p',{class:'small muted',text:`${order.id} · ${when(order.created_at)}`})]),
        el('div',{class:'order-price',text:money(order.amount_cents)})]),
      el('p',{class:'muted small',text:order.status==='pending_payment'?`模拟支付截止：${when(order.expires_at)}，超时自动释放库存。`:'全程模拟，不产生真实交易或支付流水。'}),
      el('details',{},[el('summary',{text:'查看订单费用快照'}),...q.items.map(item=>costRow(`${item.label} · ${item.service_date} ×${item.quantity}`,money(item.subtotal_cents)))]),
      el('div',{class:'order-actions'},actions)]);
  }):[el('p',{class:'empty-state',text:'还没有订单。在行程方案中确认报价后，即可创建模拟订单。'})]);
}
async function orderAction(order,action) {
  const paying=action==='pay';
  if(!await confirmAction(paying?'确认模拟支付？':'确认取消订单？',paying?`模拟支付 ${money(order.amount_cents)}，仅更新订单状态，不会真实扣款。`:
    (order.status==='confirmed'?`模拟退款 ${money(order.amount_cents)}，并释放所占库存。不会发生真实资金流转。`:'取消待支付订单，并释放所占模拟库存。')))return;
  await api(`/api/orders/${order.id}/${action}`,{method:'POST',body:{confirmed:true}});await loadOrders();toast(paying?'模拟支付成功':'订单已取消，库存已释放');
}
$('refresh-orders').addEventListener('click',safe(loadOrders));
const actionLabels={'account.registered':'创建账号','account.logged_in':'登录账号','account.logged_out':'退出账号','plan.created':'保存行程方案','plan.updated':'更新方案与报价','order.created':'创建模拟订单','order.confirmed':'完成模拟支付','order.cancelled':'取消模拟订单','order.refunded':'完成模拟退款','order.expired':'订单超时关闭','chat.completed':'完成旅行咨询','chat.failed':'旅行咨询失败','conversation.cleared':'清空会话'};
async function loadAudit() {
  const events=await api('/api/audit');replace($('audit-list'),events.length?events.map(event=>el('div',{class:'audit-row'},[
    el('div',{class:'cost-row'},[el('strong',{text:actionLabels[event.action]||event.action}),el('span',{class:'muted small',text:when(event.created_at)})]),
    el('code',{text:event.object_id})])):[el('p',{class:'empty-state',text:'暂无活动记录'})]);
}
$('refresh-audit').addEventListener('click',safe(loadAudit));
setInterval(()=>{if(state.user&&state.tab==='orders'&&!document.hidden)loadOrders().catch(()=>{});},30000);
(async()=>{try{await enter(await api('/api/auth/me'));}catch{showAuth();}})();
