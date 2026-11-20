/** 探针试跑：生成/下载数据包、执行试跑、核对快照来源、核销模板选择器。 */
import { request } from './client'

export type TrialCandidate = {
  candidate_id: string
  template_id: number
  template_name: string
  template_version: string
  site: string
  field: string
  selector: string
  sample_page: string
  status: string
  reason: string
  quota_holder: boolean
  conflicts_with: string[]
}

export type Declaration = {
  trialable: TrialCandidate[]
  pending: TrialCandidate[]
  trialable_count: number
  pending_count: number
  sampling_quota?: { per_page: number; scope: string }
}

export type SamplingConflict = {
  site: string
  sample_page: string
  quota_per_page: number
  quota_holder_template_id: number
  quota_holder_candidate_ids: string[]
  blocked_template_ids: number[]
  fields: { field: string; selectors_by_template: Record<string, string> }[]
}

export type PackageSummary = {
  ok: boolean
  package_id: string
  package_sha256: string
  created_at: string
  schema_version: string
  template_ids: number[]
  template_names: Record<string, string>
  sample_pages: string[]
  declaration: Declaration & { template_count?: number }
  conflicts: SamplingConflict[]
  candidates: TrialCandidate[]
  trialable: TrialCandidate[]
  pending: TrialCandidate[]
  download_url: string
  checksums_verified: boolean
}

export type FieldCandidate = {
  candidate_id: string
  template_id: number
  template_name: string
  template_version: string
  site: string
  field: string
  selector: string
  sample_page: string
  status: string
  reason: string
  conflicts_with: string[]
  snapshot_ids: string[]
  package_ids: string[]
  last_trial: null | {
    trial_run_id: string
    package_id: string
    snapshot_id: string
    matched: boolean
    matched_value: string
    stale_package: boolean
    at: string
  }
}

export type TrialRun = {
  run_id: string
  package_id: string
  package_sha256: string
  source: string
  started_at: string
  finished_at: string
  checksums_verified: boolean
  warnings: string[]
  declaration: Declaration & { executed_trialable_count: number; executed_pending_count: number }
  trialable: (TrialCandidate & {
    snapshot_id: number
    snapshot_code: string
    dom_hash: string
    matched: boolean
    matched_value: string
  })[]
  pending: TrialCandidate[]
  conflicts: SamplingConflict[]
  snapshots: {
    snapshot_id: number
    snapshot_code: string
    page: string
    dom_hash: string
    package_id: string
    package_sha256: string
    candidate_ids: string[]
    related_snapshots: {
      snapshot_id: number
      package_id: string
      dom_hash: string
      template_ids: number[]
    }[]
  }[]
  stale_check: {
    comparable: boolean
    stale: boolean
    summary: string
    changes: {
      template_id: number
      template_name: string
      status: string
      package_version: string
      current_version: string | null
      field_changes?: {
        added: { field: string; selector: string }[]
        removed: { field: string; selector: string }[]
        unchanged_count: number
      }
      candidate_scope?: {
        package_count: number
        current_count: number
        package_selectors: string[]
        current_selectors: string[]
      }
      sample_scope?: { package: string[]; current: string[]; added: string[]; removed: string[] }
    }[]
  }
  writeback: {
    field_candidate_count: number
    verified_count: number
    pending_count: number
    failed_count: number
    reconciled_count: number
    field_candidates: FieldCandidate[]
  }
  ok: boolean
}

export type VerificationReport = {
  ok: boolean
  template_id: number
  template_name: string
  template_version: string
  template_status: string
  fields: {
    field: string
    selector: string
    sample_page: string
    status: string
    reason: string
    snapshot_ids: string[]
    package_ids: string[]
    verified_at: string | null
  }[]
  total_selectors: number
  verified_count: number
  pending_count: number
  stale_trial_count: number
  can_reconcile: boolean
  applied?: boolean
  message?: string
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || `接口返回 ${response.status}`)
  }
  return payload as T
}

async function get<T>(path: string): Promise<T> {
  const response = await request(path)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || `接口返回 ${response.status}`)
  }
  return payload as T
}

export function previewPackage(templateIds: number[], quotaPerPage = 1) {
  return post<{
    ok: boolean
    candidates: TrialCandidate[]
    trialable: TrialCandidate[]
    pending: TrialCandidate[]
    declaration: Declaration
    sample_pages: string[]
    cross_template_conflicts: SamplingConflict[]
  }>(
    '/api/probe-trial/packages/preview',
    { template_ids: templateIds, quota_per_page: quotaPerPage },
  )
}

export async function createPackage(templateIds: number[], quotaPerPage = 1): Promise<PackageSummary> {
  return post<PackageSummary>('/api/probe-trial/packages', {
    template_ids: templateIds,
    quota_per_page: quotaPerPage,
  })
}

export async function downloadPackage(packageId: string): Promise<void> {
  const response = await request(`/api/probe-trial/packages/${packageId}/download`)
  if (!response.ok) throw new Error('数据包下载失败，请重新打包')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${packageId}.zip`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export function listPackages() {
  return get<{ items: { package_id: string; sha256: string; size: number; saved_at: string; readable: boolean }[]; total: number }>(
    '/api/probe-trial/packages',
  )
}

export function runTrial(packageId: string) {
  return post<TrialRun>('/api/probe-trial/runs', { package_id: packageId })
}

export function listRuns() {
  return get<{ items: TrialRun[]; total: number }>('/api/probe-trial/runs')
}

export function listFieldCandidates(params: { template_id?: number; status?: string } = {}) {
  const query = new URLSearchParams(
    Object.entries(params)
      .filter(([, value]) => value !== undefined && value !== '')
      .map(([key, value]) => [key, String(value)]),
  ).toString()
  return get<{ items: FieldCandidate[]; total: number; counts: Record<string, number> }>(
    `/api/probe-trial/field-candidates?${query}`,
  )
}

export function templateVerification(templateId: number) {
  return get<VerificationReport>(`/api/probe-trial/templates/${templateId}/verification`)
}

export function reconcileTemplate(templateId: number) {
  return post<VerificationReport>(`/api/probe-trial/templates/${templateId}/reconcile`, {})
}

export function snapshotProvenance(snapshotId: number) {
  return get<{
    ok: boolean
    snapshot_id: number
    page: string
    package_id: string
    dom_hash_match: boolean
    package_sha256_match: boolean
    source_consistent: boolean
    candidate_ids: string[]
    selectors: string[]
  }>(`/api/probe-trial/snapshots/${snapshotId}/provenance`)
}
