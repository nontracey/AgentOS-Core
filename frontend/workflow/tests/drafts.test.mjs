import test from 'node:test';
import assert from 'node:assert/strict';
import {DraftSession} from '../src/drafts.ts';

test('writes are serialized and retain the newest pending edit', async()=>{
  let release;
  const blocked=new Promise(resolve=>{release=resolve});
  const writes=[];
  const session=new DraftSession('draft',0,null,async(_,body)=>{
    writes.push(body);if(writes.length===1)await blocked;
    return {revision:body.expected_revision+1};
  },()=>{});
  const one=session.flush({name:'first'});
  const two=session.flush({name:'second'});
  const three=session.flush({name:'latest'});
  release();await Promise.all([one,two,three]);
  assert.deepEqual(writes.map(w=>[w.expected_revision,w.payload.name]),[[0,'first'],[1,'latest']]);
  assert.equal(session.revision,2);
  assert.equal(session.saved,JSON.stringify({name:'latest'}));
});

test('lost successful receipt is reconciled by reading the same saved snapshot',async()=>{
  let writes=0;
  const payload={name:'persisted'};
  const session=new DraftSession('draft',0,null,async(_,body)=>{
    if(body){writes++;throw Error('lost reply');}
    return {revision:1,payload};
  },()=>{});
  await session.flush(payload);
  await session.flush(payload);
  assert.equal(writes,1);assert.equal(session.revision,1);assert.equal(session.error,'');
});

test('conflicting session never silently adopts or overwrites another snapshot',async()=>{
  const expected=[];
  const session=new DraftSession('draft',1,{name:'original'},async(_,body)=>{
    if(body){expected.push(body.expected_revision);throw Error('conflict');}
    return {revision:2,payload:{name:'other session'}};
  },()=>{});
  await assert.rejects(session.flush({name:'local edit'}));
  await assert.rejects(session.flush({name:'new local edit'}));
  assert.deepEqual(expected,[1,1]);assert.equal(session.revision,1);
  assert.deepEqual(session.pending,{name:'new local edit'});
  assert.equal(session.saved,JSON.stringify({name:'original'}));
});

test('offline snapshot can be retried without changing its expected revision',async()=>{
  let online=false;
  const session=new DraftSession('draft',0,null,async(_,body)=>{
    if(!online)throw Error('offline');
    assert.equal(body.expected_revision,0);return {revision:1};
  },()=>{});
  await assert.rejects(session.flush({name:'offline edit'}));
  online=true;await session.flush({name:'offline edit'});
  assert.equal(session.revision,1);assert.equal(session.error,'');
});
