/** Serial optimistic writes, with safe reconciliation after a lost HTTP receipt. */
export type Request = (path:string, body?:unknown)=>Promise<any>;
export class DraftSession {
  revision:number;
  saved:string;
  pending:Record<string,unknown>|null=null;
  running:Promise<void>|null=null;
  error='';
  constructor(readonly id:string, revision:number, payload:Record<string,unknown>|null,
    readonly request:Request, readonly changed:()=>void) {
    this.revision=revision;this.saved=payload?JSON.stringify(payload):'';
  }
  async flush(payload:Record<string,unknown>):Promise<void> {
    this.pending=payload;
    if(this.running)return this.running;
    this.error='';
    const drain=async()=>{
      while(this.pending){
        const next=this.pending;this.pending=null;
        const signature=JSON.stringify(next);
        if(signature===this.saved)continue;
        try{
          const result=await this.request('/v1/workflow-drafts/'+encodeURIComponent(this.id),
            {expected_revision:this.revision,payload:next});
          this.revision=result.revision;this.saved=signature;
        }catch{
          // A response can be lost after committing. Read before deciding to retry.
          try{
            const latest=await this.request('/v1/workflow-drafts/'+encodeURIComponent(this.id));
            if(JSON.stringify(latest.payload)!==signature)throw Error('conflict');
            this.revision=latest.revision;this.saved=signature;
          }catch{
            this.pending=this.pending||next;
            this.error='草稿未确认保存：连接失败或其他页面已修改。可重试保存，或导出后重新打开服务端草稿。';
            throw Error(this.error);
          }
        }
        this.changed();
      }
    };
    this.running=drain();this.changed();
    try{await this.running;}finally{this.running=null;this.changed();}
  }
}
