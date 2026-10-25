"use strict";
const $ = id => document.getElementById(id);
let csrf = "";
let allTickets = [];
let allSummary = {};
const fmt = amount => (amount / 100).toLocaleString("de-DE", {style:"currency",currency:"EUR"});
const esc = str => {const x=document.createElement("span");x.textContent=String(str ?? "");return x.innerHTML;};
const statusNames = {open:"Offen",won:"Gewonnen",lost:"Verloren",push:"Erstattet",needs_review:"Prüfung nötig"};
const dateLabel = iso => {try{return new Date(iso).toLocaleString("de-DE",{dateStyle:"short",timeStyle:"short"});}catch{return iso;}};
function localNow(){const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
async function api(path, payload){
  const options={credentials:"same-origin",cache:"no-store"};
  if(payload!==undefined){options.method="POST";options.headers={"Content-Type":"application/json"};options.body=JSON.stringify({...payload,csrf});}
  const response=await fetch("/api/wetten/"+path,options);
  const data=await response.json();
  if(!response.ok)throw Error(data.error||"Anfrage fehlgeschlagen");
  return data;
}
function showWorkspace(show){
  $("workspace").hidden=!show;$("login-panel").hidden=show;
}
async function session(){
  const s=await api("session");
  csrf=s.csrf||"";
  showWorkspace(s.authenticated);
  if(!s.configured)$("login-info").textContent="Die Serverkonfiguration WETTEN_PASSWORD fehlt. Die privaten Wettendaten bleiben gesperrt.";
  if(s.authenticated)await load();
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=event.target.querySelector("button");button.disabled=true;
  try{const s=await api("login",{password:$("password").value});csrf=s.csrf;$("password").value="";showWorkspace(true);await load();}
  catch(e){$("login-info").textContent=e.message;}finally{button.disabled=false;}
});
$("logout").addEventListener("click",async()=>{
  try{await api("logout",{});}catch(e){console.warn("Abmeldung",e.message);}
  csrf="";allTickets=[];showWorkspace(false);
});
function addLeg(){
  const template=$("leg-template").content.cloneNode(true);
  template.querySelector(".remove-leg").onclick=event=>{
    const box=event.target.closest(".leg");
    if($("legs").querySelectorAll(".leg").length>1)box.remove();
  };
  $("legs").appendChild(template);
}
$("add-leg").onclick=addLeg;addLeg();
$("create-form").elements.namedItem("placed_at").value=localNow();
$("create-form").addEventListener("submit",async event=>{
  event.preventDefault();const form=event.currentTarget;const button=form.querySelector('button[type="submit"]');
  const get=(el,name)=>el.querySelector('[name="'+name+'"]').value;
  const legs=[...$("legs").querySelectorAll(".leg")].map(el=>{
    const fields=["sport","league","home","away","kickoff","market","selection","line","odds","period","result_url"];
    const result={};fields.forEach(f=>result[f]=get(el,f));
    return result;
  });
  const entry={
    bookmaker:get(form,"bookmaker"),placed_at:new Date(get(form,"placed_at")).toISOString(),
    stake:get(form,"stake"),note:get(form,"note"),legs
  };
  button.disabled=true;
  try{await api("create",entry);$("form-status").textContent="Wette gespeichert.";form.reset();$("legs").textContent="";addLeg();form.elements.namedItem("placed_at").value=localNow();await load();}
  catch(e){$("form-status").textContent=e.message;}
  finally{button.disabled=false;}
});
function calculate(tickets){
  const settled=tickets.filter(t=>["won","lost","push"].includes(t.status));
  return {
    total:tickets.length,
    stake:tickets.reduce((n,t)=>n+t.stake_cents,0),
    open:tickets.filter(t=>!["won","lost","push"].includes(t.status)).reduce((n,t)=>n+t.stake_cents,0),
    profit:settled.reduce((n,t)=>n+(t.payout_cents||0)-t.stake_cents,0),
    settledStake:settled.reduce((n,t)=>n+t.stake_cents,0),
    winCount:settled.filter(t=>t.status==="won").length,
    decidedCount:settled.filter(t=>t.status==="won"||t.status==="lost").length
  };
}
function render(){
  const bookmaker=$("bookmaker-filter").value,period=$("period-filter").value;
  const today=new Date().toLocaleDateString("sv-SE");
  const thisMonth=today.slice(0,7);
  const list=allTickets.filter(t=>
    (bookmaker==="all"||t.bookmaker===bookmaker)&&
    (period==="all" || (period==="today"&&new Date(t.placed_at).toLocaleDateString("sv-SE")===today)||
    (period==="month"&&new Date(t.placed_at).toLocaleDateString("sv-SE").slice(0,7)===thisMonth))
  );
  const m=calculate(list);
  const roi=m.settledStake?((100*m.profit/m.settledStake).toFixed(1)+" %"):"–";
  const hit=m.decidedCount?((100*m.winCount/m.decidedCount).toFixed(1)+" %"):"–";
  const cards=[
    ["Wetten",m.total],["Gesamteinsätze",fmt(m.stake)],["Offener Einsatz",fmt(m.open)],
    ["Realisierter Gewinn/Verlust",fmt(m.profit)],["ROI / Trefferquote",roi+" / "+hit]
  ];
  $("kpis").innerHTML=cards.map(([title,value],i)=>'<div class="kpi"><small>'+esc(title)+'</small><strong class="'+(i===3?(m.profit>=0?"positive":"negative"):"")+'">'+esc(value)+'</strong></div>').join("");
  $("count").textContent=list.length+" Einträge";
  $("tickets").innerHTML=list.length?list.map(ticket=>{
    const legs=ticket.legs.map(l=>'<div class="leg-row">'+esc(l.home)+' – '+esc(l.away)+': <b>'+esc(l.selection)+'</b> ('+esc(l.market)+(l.line?" "+esc(l.line):"")+') @ '+esc(l.odds)+' · <span class="pill '+esc(l.status)+'">'+esc(statusNames[l.status]||l.status)+'</span> <small>'+esc(l.home_score===null?"":"Endstand "+l.home_score+":"+l.away_score)+'</small></div>').join("");
    return '<article class="ticket"><div class="ticket-title"><h3>'+esc(ticket.bookmaker)+' · '+esc(ticket.type==="combo"?"Kombiwette":"Einzelwette")+' #'+ticket.id+'</h3><span class="pill '+esc(ticket.status)+'">'+esc(statusNames[ticket.status]||ticket.status)+'</span></div>'+
      '<div class="ticket-details"><span>'+esc(dateLabel(ticket.placed_at))+'</span><span>Einsatz <b>'+fmt(ticket.stake_cents)+'</b></span><span>Quote '+esc(ticket.odds)+'</span><span>Mögliche Auszahlung '+fmt(ticket.potential_cents)+'</span>'+
      (ticket.payout_cents!==null?'<span>Tatsächliche Auszahlung '+fmt(ticket.payout_cents)+'</span>':"")+'</div>'+
      '<div>'+legs+'</div><div class="ticket-actions"><button class="secondary" data-action="settle" data-id="'+ticket.id+'">Manuell abrechnen</button><button class="secondary" data-action="delete" data-id="'+ticket.id+'">Löschen</button></div></article>';
  }).join(""):'<div class="empty">Keine Wetten für diese Auswahl vorhanden.</div>';
}
$("tickets").addEventListener("click",async event=>{
  const button=event.target.closest("button[data-action]");
  if(!button)return;
  const id=Number(button.dataset.id),action=button.dataset.action;
  try{
    if(action==="delete"){
      if(!confirm("Wette #"+id+" wirklich löschen?"))return;
      await api("delete",{id});
    }else if(action==="settle"){
      const status=prompt("Status: won / lost / push / open / needs_review","won");
      if(!status)return;
      const reason=prompt("Grund / Buchmacher-Abrechnung dokumentieren:","");
      if(!reason)return;
      let payout;
      if(status==="won"){payout=prompt("Tatsächlich ausbezahlter Betrag (€):","");if(!payout)return;}
      await api("settle",{id,status,payout,reason});
    }
    await load();
  }catch(e){alert(e.message);}
});
$("bookmaker-filter").onchange=render;$("period-filter").onchange=render;
$("reload").onclick=load;
async function load(){
  try{
    const data=await api("data");
    allTickets=data.tickets||[];allSummary=data.summary||{};
    const poll=data.poller||{};
    $("poller").textContent="Ergebnisprüfung: "+(poll.result==="disabled_no_approved_html_sources"?"Keine HTML-Quellen freigegeben":poll.result||"Nicht gestartet")+" · zuletzt "+(poll.at?dateLabel(poll.at):"nie")+". Aktualisierung: 5 Minuten.";
    render();
  }catch(e){
    $("poller").textContent="Datenabruf fehlgeschlagen: "+e.message;
    if(e.message==="Nicht angemeldet")showWorkspace(false);
  }
}
setInterval(()=>{if(!$("workspace").hidden)load();},300000);
session().catch(e=>{$("login-info").textContent="Verbindung fehlgeschlagen: "+e.message;});
