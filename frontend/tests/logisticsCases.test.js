import { test } from 'node:test'
import assert from 'node:assert/strict'
import { requestChat, requestLogisticsCase } from '../src/lib/backends.js'
const settings = { backend: 'python', userId: 'demo-user', conversationId: 'case-conv', pythonToken: '', endpoints: {python:'/api/python',java:'/api/java'} }

test('chat preserves structured case and carries conversation identifier', async () => {
  const saved = globalThis.fetch
  try {
    const sample = {case_id:'LC-1',state:'awaiting_confirmation',can_confirm:true,source:'mock'}
    globalThis.fetch = async (url, options) => {
      const body = JSON.parse(options.body)
      assert.equal(body.conv_id, 'case-conv')
      assert.equal(body.message, '确认建单')
      assert.ok(body.client_request_id)
      return new Response(JSON.stringify({conv_id:'case-conv',response:'待确认',logistics_case:sample}))
    }
    const response = await requestChat('python',settings,'确认建单')
    assert.deepEqual(response.logisticsCase,sample)
  } finally {globalThis.fetch=saved}
})

test('case restoration scopes request by user and conversation, skips unsupported backend', async () => {
  const saved=globalThis.fetch
  try {
    let calls=0
    globalThis.fetch=async url=>{
      calls++
      assert.ok(url.includes('user_id=demo-user'))
      assert.ok(url.includes('conv_id=case-conv'))
      return new Response(JSON.stringify({logistics_case:{state:'ticket_created',can_confirm:false}}))
    }
    assert.equal((await requestLogisticsCase('python',settings)).can_confirm,false)
    assert.equal(await requestLogisticsCase('java',settings),null)
    assert.equal(await requestLogisticsCase('python',{...settings,conversationId:''}),null)
    assert.equal(calls,1)
  } finally {globalThis.fetch=saved}
})
