<template>
  <section class="page" data-module="probe_trial">
    <header class="page-head">
      <div>
        <h2>探针试跑</h2>
        <p class="page-desc">
          把已验证模板的探针候选导出为数据包，采集端按样本页试跑后回写字段候选清单，模板规则据此核销选择器。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="trial-refresh" type="button" @click="reloadAll">刷新清单</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="trial-error">{{ errorMessage }}</p>
    <p v-if="noticeMessage" class="notice-text" data-testid="trial-notice">{{ noticeMessage }}</p>

    <section class="panel" data-testid="trial-package-panel">
      <h3>① 生成探针试跑数据包</h3>
      <form class="filter-bar" @submit.prevent="previewPackages">
        <label class="filter-item">
          <span>本轮采样额度（每站点每样本页）</span>
          <input v-model.number="quotaPerPage" type="number" min="1" max="20" data-testid="trial-quota" />
        </label>
        <button class="btn" data-testid="trial-preview" type="submit" :disabled="busy">预览候选与额度</button>
        <button class="btn primary" data-testid="trial-create" type="button" :disabled="busy || !selectedIds.length" @click="buildPackage">
          生成并落盘数据包
        </button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th>
            <th>模板</th>
            <th>适用站点</th>
            <th>提取字段</th>
            <th>字段选择器</th>
            <th>采样页面</th>
            <th>当前状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in templates" :key="row.id" :data-testid="`trial-template-${row.id}`">
            <td>
              <input v-model="selectedIds" type="checkbox" :value="row.id" :data-testid="`trial-select-${row.id}`" />
            </td>
            <td>{{ row['模板名称'] }}</td>
            <td>{{ row['适用站点'] }}</td>
            <td>{{ row['提取字段'] }}</td>
            <td>{{ row['字段选择器'] }}</td>
            <td>{{ row['采样页面'] }}</td>
            <td>{{ row.status }}</td>
          </tr>
          <tr v-if="!templates.length">
            <td colspan="7" class="empty-state">暂无模板，请先在模板规则模块登记</td>
          </tr>
        </tbody>
      </table>

      <div v-if="conflicts.length" class="notice-block" data-testid="trial-conflicts">
        <strong>采样额度冲突</strong>
        <ul>
          <li v-for="(conflict, index) in conflicts" :key="`${conflict.sample_page}-${index}`">
            {{ conflict.site }} · {{ conflict.sample_page }}：本轮额度 {{ conflict.quota_per_page }}，
            分配给模板 {{ conflict.quota_holder_template_id }}；
            模板 {{ conflict.blocked_template_ids.join('、') }} 的候选转为待验证。
            <span v-for="field in conflict.fields" :key="field.field" class="conflict-field">
              {{ field.field }}：
              <template v-for="(selector, tid) in field.selectors_by_template" :key="tid">
                模板{{ tid }}={{ selector }}；
              </template>
            </span>
          </li>
        </ul>
      </div>
    </section>

    <section v-if="latestPackage" class="panel" data-testid="trial-package-result">
      <h3>② 交付声明</h3>
      <p class="status-legend">
        数据包 <code>{{ latestPackage.package_id }}</code> ·
        校验和 <code>{{ latestPackage.package_sha256.slice(0, 16) }}…</code> ·
        schema {{ latestPackage.schema_version }} ·
        {{ latestPackage.checksums_verified ? '文件校验已通过' : '未校验' }}
      </p>
      <div class="page-actions">
        <button class="btn" data-testid="trial-download" type="button" :disabled="busy" @click="downloadLatest">
          下载数据包
        </button>
        <button class="btn primary" data-testid="trial-run" type="button" :disabled="busy" @click="startTrial">
          用该包试跑
        </button>
      </div>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">本轮可试跑候选</span>
          <strong class="stat-value" data-testid="trial-trialable-count">{{ latestPackage.declaration.trialable_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">留下待验证</span>
          <strong class="stat-value" data-testid="trial-pending-count">{{ latestPackage.declaration.pending_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">样本页</span>
          <strong class="stat-value">{{ latestPackage.sample_pages.length }}</strong>
        </article>
      </div>

      <table class="data-table">
        <thead>
          <tr><th>候选</th><th>模板</th><th>字段</th><th>选择器</th><th>样本页</th><th>状态</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="candidate in latestPackage.candidates" :key="candidate.candidate_id" :data-testid="`trial-candidate-${candidate.candidate_id}`">
            <td><code>{{ candidate.candidate_id }}</code></td>
            <td>{{ candidate.template_id }} · {{ candidate.template_name }}</td>
            <td>{{ candidate.field }}</td>
            <td><code>{{ candidate.selector || '—' }}</code></td>
            <td>{{ candidate.sample_page || '—' }}</td>
            <td>{{ candidate.status }}</td>
            <td>{{ candidate.reason || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="latestRun" class="panel" data-testid="trial-run-result">
      <h3>③ 试跑结论与快照来源</h3>
      <p class="status-legend" data-testid="trial-run-summary">
        试跑 {{ latestRun.run_id }} · 包 <code>{{ latestRun.package_id }}</code> ·
        已试跑 {{ latestRun.declaration.executed_trialable_count }} ·
        回写候选 {{ latestRun.writeback.field_candidate_count }} ·
        已验证 {{ latestRun.writeback.verified_count }} · 待验证 {{ latestRun.writeback.pending_count }}
      </p>
      <p v-if="latestRun.stale_check.comparable" class="notice-block" data-testid="trial-stale">
        {{ latestRun.stale_check.summary }}
        <template v-for="change in latestRun.stale_check.changes" :key="change.template_id">
          <br />
          模板 {{ change.template_id }}：候选 {{ change.candidate_scope?.package_count || 0 }} →
          {{ change.candidate_scope?.current_count || 0 }}；新增
          {{ (change.field_changes?.added || []).map((item) => `${item.field}=${item.selector}`).join('、') || '无' }}；
          移除
          {{ (change.field_changes?.removed || []).map((item) => `${item.field}=${item.selector}`).join('、') || '无' }}；
          样本范围 {{ change.sample_scope?.package.join('、') || '—' }} →
          {{ change.sample_scope?.current.join('、') || '—' }}
        </template>
      </p>

      <table class="data-table">
        <thead>
          <tr><th>字段</th><th>选择器</th><th>样本页</th><th>快照</th><th>DOM 哈希</th><th>试跑</th><th>同页其他快照</th></tr>
        </thead>
        <tbody>
          <tr v-for="candidate in latestRun.trialable" :key="candidate.candidate_id" :data-testid="`trial-result-${candidate.candidate_id}`">
            <td>{{ candidate.field }}</td>
            <td><code>{{ candidate.selector }}</code></td>
            <td>{{ candidate.sample_page }}</td>
            <td><code>{{ candidate.snapshot_code }}</code></td>
            <td><code>{{ candidate.dom_hash }}</code></td>
            <td>
              <span :class="candidate.matched ? 'ok-text' : 'error-text'">{{ candidate.matched ? '命中' : '未命中' }}</span>
              <button class="link" type="button" :data-testid="`trial-provenance-${candidate.snapshot_id}`" @click="checkProvenance(candidate.snapshot_id)">
                核对来源
              </button>
            </td>
            <td>
              <span v-for="related in relatedSnapshots(candidate.sample_page)" :key="related.snapshot_id">
                {{ related.package_id }}({{ related.dom_hash }})
              </span>
              <span v-if="!relatedSnapshots(candidate.sample_page).length">—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <p v-if="provenanceResult" class="status-legend" data-testid="trial-provenance-result">
        快照 {{ provenanceResult.snapshot_id }} 来源核对：
        {{ provenanceResult.source_consistent ? '一致' : '不一致' }} ·
        包 {{ provenanceResult.package_id }} ·
        DOM 哈希{{ provenanceResult.dom_hash_match ? '匹配' : '不匹配' }} ·
        包文件{{ provenanceResult.package_sha256_match ? '校验通过' : '不可用' }}
      </p>
    </section>

    <section class="panel" data-testid="trial-ledger-panel">
      <h3>④ 字段候选清单（模板规则核销依据）</h3>
      <p class="status-legend">
        可试跑 {{ counts['可试跑'] || 0 }} · 待验证 {{ counts['待验证'] || 0 }} ·
        已验证 {{ counts['已验证'] || 0 }} · 试跑失败 {{ counts['试跑失败'] || 0 }} ·
        已核销 {{ counts['已核销'] || 0 }}
      </p>
      <table class="data-table">
        <thead>
          <tr><th>候选</th><th>模板</th><th>字段</th><th>选择器</th><th>样本页</th><th>状态</th><th>快照</th><th>来源包</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in fieldCandidates" :key="item.candidate_id" :data-testid="`trial-ledger-${item.candidate_id}`">
            <td><code>{{ item.candidate_id }}</code></td>
            <td>{{ item.template_id }} · {{ item.template_name }}</td>
            <td>{{ item.field }}</td>
            <td><code>{{ item.selector }}</code></td>
            <td>{{ item.sample_page }}</td>
            <td :data-status="item.status">{{ item.status }}</td>
            <td>{{ item.snapshot_ids.join('、') || '—' }}</td>
            <td>{{ item.package_ids.join('、') || '—' }}</td>
          </tr>
          <tr v-if="!fieldCandidates.length">
            <td colspan="8" class="empty-state">还没有字段候选，请先生成并试跑数据包</td>
          </tr>
        </tbody>
      </table>

      <div class="filter-bar">
        <label class="filter-item">
          <span>核销模板</span>
          <select v-model.number="reconcileTemplateId" data-testid="trial-reconcile-template">
            <option :value="0" disabled>选择模板</option>
            <option v-for="row in templates" :key="row.id" :value="row.id">
              {{ row.id }} · {{ row['模板名称'] }}
            </option>
          </select>
        </label>
        <button class="btn" data-testid="trial-verify-template" type="button" :disabled="!reconcileTemplateId || busy" @click="checkVerification">
          查看核销报告
        </button>
        <button class="btn primary" data-testid="trial-reconcile" type="button" :disabled="!reconcileTemplateId || busy" @click="applyReconcile">
          核销已验证选择器
        </button>
      </div>
      <p v-if="verification" class="status-legend" data-testid="trial-verification">
        模板 {{ verification.template_id }} 版本 {{ verification.template_version }}：
        已通过 {{ verification.verified_count }} / {{ verification.total_selectors }}，
        待验证 {{ verification.pending_count }}，
        {{ verification.can_reconcile ? '可核销' : '暂不能核销' }}。
        {{ verification.message || '' }}
      </p>
      <table v-if="verification" class="data-table">
        <thead>
          <tr><th>字段</th><th>选择器</th><th>状态</th><th>说明</th><th>来源包</th></tr>
        </thead>
        <tbody>
          <tr v-for="field in verification.fields" :key="`${field.field}-${field.selector}`">
            <td>{{ field.field }}</td>
            <td><code>{{ field.selector }}</code></td>
            <td>{{ field.status }}</td>
            <td>{{ field.reason || '—' }}</td>
            <td>{{ field.package_ids.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  createPackage,
  downloadPackage,
  listFieldCandidates,
  listPackages,
  previewPackage,
  reconcileTemplate,
  runTrial,
  snapshotProvenance,
  templateVerification,
} from '@/api/probe-trial'
import type {
  FieldCandidate,
  PackageSummary,
  SamplingConflict,
  TrialRun,
  VerificationReport,
} from '@/api/probe-trial'
import type { EntryRow } from '@/data/types'

const templates = ref<EntryRow[]>([])
const selectedIds = ref<number[]>([])
const quotaPerPage = ref(1)
const conflicts = ref<SamplingConflict[]>([])
const latestPackage = ref<PackageSummary | null>(null)
const latestRun = ref<TrialRun | null>(null)
const fieldCandidates = ref<FieldCandidate[]>([])
const counts = ref<Record<string, number>>({})
const verification = ref<VerificationReport | null>(null)
const reconcileTemplateId = ref(0)
const provenanceResult = ref<Awaited<ReturnType<typeof snapshotProvenance>> | null>(null)
const errorMessage = ref('')
const noticeMessage = ref('')
const busy = ref(false)

function relatedSnapshots(page: string) {
  const snapshots = latestRun.value?.snapshots.filter((item) => item.page === page) ?? []
  return snapshots.flatMap((item) => item.related_snapshots)
}

function setError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function loadTemplates() {
  const payload = await listEntries('template_rule')
  templates.value = payload.items
  if (!selectedIds.value.length) {
    const defaults = payload.items
      .filter((row) => ['已发布', '已验证'].includes(String(row.status)))
      .slice(0, 2)
      .map((row) => Number(row.id))
    selectedIds.value = defaults.length ? defaults : payload.items.slice(0, 2).map((row) => Number(row.id))
  }
}

async function loadLedger() {
  const payload = await listFieldCandidates()
  fieldCandidates.value = payload.items
  counts.value = payload.counts
}

async function loadPackageList() {
  await listPackages()
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    await loadTemplates()
    await loadLedger()
    await loadPackageList()
  } catch (error) {
    setError(error, '探针试跑数据读取失败')
  }
}

async function previewPackages() {
  if (!selectedIds.value.length) {
    errorMessage.value = '请先选择至少一个模板'
    return
  }
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await previewPackage(selectedIds.value, quotaPerPage.value)
    conflicts.value = payload.cross_template_conflicts
    noticeMessage.value = conflicts.value.length
      ? '存在采样额度冲突，冲突模板的候选会以“待验证”写进数据包'
      : '本轮候选均可试跑'
  } catch (error) {
    setError(error, '预览失败')
  } finally {
    busy.value = false
  }
}

async function buildPackage() {
  busy.value = true
  errorMessage.value = ''
  try {
    latestPackage.value = await createPackage(selectedIds.value, quotaPerPage.value)
    conflicts.value = latestPackage.value.conflicts
    noticeMessage.value = `数据包 ${latestPackage.value.package_id} 已落盘，可下载交付采集端`
    await loadLedger()
  } catch (error) {
    setError(error, '生成数据包失败')
  } finally {
    busy.value = false
  }
}

async function downloadLatest() {
  if (!latestPackage.value) return
  busy.value = true
  try {
    await downloadPackage(latestPackage.value.package_id)
  } catch (error) {
    setError(error, '数据包下载失败')
  } finally {
    busy.value = false
  }
}

async function startTrial() {
  if (!latestPackage.value) return
  busy.value = true
  errorMessage.value = ''
  provenanceResult.value = null
  try {
    latestRun.value = await runTrial(latestPackage.value.package_id)
    noticeMessage.value = `试跑完成，结论已回写 ${latestRun.value.writeback.field_candidate_count} 条候选`
    await loadLedger()
  } catch (error) {
    setError(error, '试跑失败')
  } finally {
    busy.value = false
  }
}

async function checkProvenance(snapshotId: number) {
  errorMessage.value = ''
  try {
    provenanceResult.value = await snapshotProvenance(snapshotId)
  } catch (error) {
    setError(error, '快照来源核对失败')
  }
}

async function checkVerification() {
  if (!reconcileTemplateId.value) return
  errorMessage.value = ''
  try {
    verification.value = await templateVerification(reconcileTemplateId.value)
  } catch (error) {
    setError(error, '核销报告读取失败')
  }
}

async function applyReconcile() {
  if (!reconcileTemplateId.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    verification.value = await reconcileTemplate(reconcileTemplateId.value)
    noticeMessage.value = verification.value.message || '核销完成'
    await loadTemplates()
    await loadLedger()
  } catch (error) {
    setError(error, '核销失败')
  } finally {
    busy.value = false
  }
}

onMounted(reloadAll)
</script>
