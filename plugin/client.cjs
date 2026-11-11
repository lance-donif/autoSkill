const React=require('react');
exports.inject=['slots','layout'];
exports.apply=function(ctx,config={}){
 const port=config.port??8787;
 if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('Invalid autoSkill port');
 const origin='http://127.0.0.1:'+port;
 for(const [key,label,page] of [['autoskill-dashboard','autoSkill 仪表盘','dashboard'],['autoskill-schedules','autoSkill 定时任务','schedules']]){
  const Panel=()=>React.createElement('iframe',{src:origin+'/?embed=1#/'+page,title:label,style:{width:'100%',height:'100%',border:0,display:'block',background:'#f5f7f8'}});
  const Icon=()=>React.createElement('span',{'aria-hidden':true,style:{fontSize:15}},page==='dashboard'?'▦':'◷');
  ctx.effect(()=>ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key},Panel)),'autoskill: '+key);
  ctx.effect(()=>ctx.slots.inject('sidebar.panellist',()=>ctx.slots.register({name:'sidebar.panellist',id:key,order:30,label:()=>label},Icon)),'autoskill: nav '+key);
 }
};
