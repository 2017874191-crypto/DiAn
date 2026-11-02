<template>
  <section class="logistics-case" aria-label="物流异常案件" aria-live="polite">
    <div class="case-heading">
      <div><span class="case-eyebrow">物流异常案件 · 模拟服务</span><h2>{{ issueLabel }}</h2></div>
      <span class="case-status" :class="{ complete: caseData.state === 'ticket_created' }">{{ stateLabel }}</span>
    </div>
    <dl class="case-facts">
      <div><dt>订单号</dt><dd>{{ caseData.order_id || '待补充' }}</dd></div>
      <div><dt>运单号</dt><dd>{{ caseData.tracking_no || '待补充' }}</dd></div>
      <div v-if="caseData.ticket_id"><dt>模拟工单</dt><dd>{{ caseData.ticket_id }}</dd></div>
    </dl>
    <p class="case-next">{{ caseData.next_step }}</p>
    <div class="case-actions">
      <button v-if="caseData.can_confirm && caseData.state === 'awaiting_confirmation'" :disabled="busy" @click="$emit('action', '确认建单')">{{ busy ? '正在处理…' : '确认创建模拟工单' }}</button>
      <button v-if="caseData.state === 'awaiting_evidence'" :disabled="busy" class="case-secondary" @click="$emit('action', '已上传外包装、面单和破损照片')">登记已提交材料声明</button>
      <button v-if="['awaiting_info', 'awaiting_evidence', 'awaiting_confirmation'].includes(caseData.state)" :disabled="busy" class="case-secondary" @click="$emit('action', '转人工')">转人工核查</button>
    </div>
    <p class="case-disclaimer">{{ caseData.state === 'awaiting_evidence' ? '当前仅登记您的材料提交声明，不会上传或审核图片。' : '所有物流与工单均为模拟数据，不执行真实赔偿或退款。' }}</p>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ caseData: { type: Object, required: true }, busy: Boolean })
defineEmits(['action'])
const issueLabel = computed(() => ({ logistics_lost: '疑似丢件核查', logistics_dispute: '签收争议核查', logistics_damaged: '破损与错发核查' })[props.caseData.issue] || '物流异常核查')
const stateLabel = computed(() => ({ awaiting_info: '待补充信息', verifying: '核验中', awaiting_evidence: '待举证', awaiting_confirmation: '待确认建单', manual_review: '待人工核查', ticket_created: '已创建模拟工单' })[props.caseData.state] || '处理中')
</script>
