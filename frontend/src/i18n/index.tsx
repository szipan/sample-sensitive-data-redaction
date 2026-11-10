/**
 * UI internationalisation (简体中文 / English).
 *
 * Same shape as the kiro-dashboard-public reference: one dictionary of
 * `key -> { zh, en }`, a `LanguageProvider` that remembers the choice in
 * localStorage, and a `useI18n()` hook returning `{ lang, setLang, t }`.
 *
 * Differences from the reference:
 *  - Keys are a compile-time union (`MessageKey`), so a typo in `t('...')` is
 *    a type error instead of silently rendering the key.
 *  - `useI18n()` works without a provider and then behaves as Chinese, the
 *    default language. Components can therefore be rendered on their own in
 *    tests without wrapping them.
 *
 * Only static UI text is translated. File names, detected values and their
 * context, entity-type labels (Debug mode) and regular expressions are data
 * and are shown as they are.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

export type Lang = 'zh' | 'en'

const STORAGE_KEY = 'pii-redaction.lang'
const DEFAULT_LANG: Lang = 'zh'

/** Languages offered in the switcher (label is always in its own language). */
export const LANGUAGES: ReadonlyArray<{ value: Lang; label: string }> = [
  { value: 'zh', label: '简体中文' },
  { value: 'en', label: 'English' },
]

const DICT = {
  // App shell
  'app.title': { zh: 'PII 文档脱敏', en: 'PII Document Redaction' },
  'app.language': { zh: '语言', en: 'Language' },
  'nav.document': { zh: '文档处理', en: 'Documents' },
  'nav.rules': { zh: '规则管理', en: 'Rules' },
  'nav.users': { zh: '用户管理', en: 'Users' },

  // Common
  'common.back': { zh: '返回', en: 'Back' },

  // --- Sign-in ---
  'auth.title': { zh: '登录', en: 'Sign in' },
  'auth.email': { zh: '邮箱', en: 'Email' },
  'auth.password': { zh: '密码', en: 'Password' },
  'auth.signIn': { zh: '登录', en: 'Sign in' },
  'auth.signingIn': { zh: '登录中…', en: 'Signing in…' },
  'auth.signOut': { zh: '退出登录', en: 'Sign out' },
  'auth.newPasswordTitle': { zh: '设置新密码', en: 'Set a new password' },
  'auth.newPasswordHint': {
    zh: '首次登录需要设置新密码：至少 12 位，包含大写字母、小写字母和数字。',
    en: 'Set a new password to finish signing in: at least 12 characters with upper-case, lower-case letters and digits.',
  },
  'auth.newPassword': { zh: '新密码', en: 'New password' },
  'auth.confirmPassword': { zh: '确认新密码', en: 'Confirm new password' },
  'auth.setPassword': { zh: '设置密码并登录', en: 'Set password and sign in' },
  'auth.err.credentials': { zh: '邮箱或密码不正确。', en: 'Incorrect email or password.' },
  'auth.err.resetRequired': {
    zh: '需要重置密码，请联系管理员。',
    en: 'Your password must be reset. Please contact an administrator.',
  },
  'auth.err.passwordPolicy': {
    zh: '新密码不符合要求：至少 12 位，包含大写字母、小写字母和数字。',
    en: 'The new password does not meet the policy: at least 12 characters with upper-case, lower-case letters and digits.',
  },
  'auth.err.mismatch': { zh: '两次输入的密码不一致。', en: 'The passwords do not match.' },
  'auth.err.tooMany': { zh: '尝试次数过多，请稍后再试。', en: 'Too many attempts. Please try again later.' },
  'auth.err.network': { zh: '无法连接登录服务，请检查网络。', en: 'Could not reach the sign-in service.' },
  'auth.err.generic': { zh: '登录失败，请重试。', en: 'Sign-in failed. Please try again.' },
  // --- User management (administrators) ---
  'users.title': { zh: '用户管理', en: 'User Management' },
  'users.rolesHint': {
    zh: '管理员可以管理用户和脱敏规则；普通用户只能上传文档并处理自己的任务。',
    en: 'Administrators manage users and redaction rules; users upload documents and work with their own jobs.',
  },
  'users.col.email': { zh: '邮箱', en: 'Email' },
  'users.col.role': { zh: '角色', en: 'Role' },
  'users.col.status': { zh: '状态', en: 'Status' },
  'users.col.actions': { zh: '操作', en: 'Actions' },
  'users.role.admin': { zh: '管理员', en: 'Administrator' },
  'users.role.user': { zh: '普通用户', en: 'User' },
  'users.status.invited': { zh: '已邀请，未首次登录', en: 'Invited, not signed in yet' },
  'users.status.active': { zh: '已激活', en: 'Active' },
  'users.you': { zh: '(你)', en: '(you)' },
  'users.makeAdmin': { zh: '设为管理员', en: 'Make administrator' },
  'users.makeUser': { zh: '设为普通用户', en: 'Make user' },
  'users.delete': { zh: '删除', en: 'Delete' },
  'users.resend': { zh: '重发邀请', en: 'Resend invitation' },
  'users.inviteTitle': { zh: '邀请用户', en: 'Invite a user' },
  'users.invite': { zh: '发送邀请', en: 'Send invitation' },
  'users.inviteHint': {
    zh: '对方会收到一封含临时密码的邮件，临时密码 3 天内有效，首次登录需设置新密码。',
    en: 'They receive an email with a temporary password, valid for 3 days, and set a new password at first sign-in.',
  },
  'users.invited': { zh: '已向 {email} 发送邀请。', en: 'Invitation sent to {email}.' },
  'users.resent': { zh: '已向 {email} 重新发送邀请。', en: 'Invitation resent to {email}.' },
  'users.roleChanged': {
    zh: '已更新 {email} 的角色，对方重新登录后生效。',
    en: 'Role of {email} updated; it applies after they sign in again.',
  },
  'users.deleted': { zh: '已删除 {email}。', en: 'Deleted {email}.' },
  'users.confirmDelete': { zh: '确定删除用户 {email}？', en: 'Delete user {email}?' },
  'users.err.INVALID_EMAIL': { zh: '邮箱地址无效。', en: 'Invalid email address.' },
  'users.err.INVALID_ROLE': { zh: '角色无效。', en: 'Invalid role.' },
  'users.err.USER_EXISTS': { zh: '该邮箱的用户已存在。', en: 'A user with this email already exists.' },
  'users.err.USER_NOT_FOUND': { zh: '用户不存在。', en: 'User not found.' },
  'users.err.CANNOT_MODIFY_SELF': {
    zh: '不能修改或删除自己的账号。',
    en: 'You cannot change or delete your own account.',
  },
  'users.err.LAST_ADMIN': {
    zh: '不能移除最后一位管理员。',
    en: 'The last administrator cannot be removed.',
  },
  'users.err.ALREADY_ACTIVE': {
    zh: '该用户已设置过密码，无需重发邀请。',
    en: 'This user has already set a password; no invitation is needed.',
  },
  'users.err.FORBIDDEN': { zh: '只有管理员可以管理用户。', en: 'Only administrators can manage users.' },
  'users.err.generic': { zh: '操作失败，请重试。', en: 'The operation failed. Please try again.' },
  'rules.adminOnly': {
    zh: '只有管理员可以新建、启用/禁用或删除规则。',
    en: 'Only administrators can create, enable/disable or delete rules.',
  },
  'common.listSeparator': { zh: '、', en: ', ' },

  // Upload view
  'upload.title': { zh: '文档处理', en: 'Document Processing' },
  'upload.mode': { zh: '处理模式', en: 'Processing mode' },
  'upload.addressSource': {
    zh: '图片/文档中地址的识别来源',
    en: 'Address detection source (images/documents)',
  },
  'upload.nameSource': {
    zh: '图片/文档中姓名的识别来源',
    en: 'Name detection source (images/documents)',
  },
  'upload.chooseFile': { zh: '选择要上传的文档', en: 'Select a document to upload' },
  'upload.supported': { zh: '支持 .docx 或 .pdf', en: 'Supports .docx or .pdf' },
  'upload.status': { zh: '状态', en: 'Status' },
  'upload.uploading': { zh: '正在上传…', en: 'Uploading…' },
  'upload.jobRef': { zh: 'Job 引用', en: 'Job ID' },
  'upload.download': { zh: '下载脱敏结果', en: 'Download redacted document' },
  'upload.downloading': { zh: '下载中…', en: 'Downloading…' },
  'upload.failed': { zh: '上传失败，请重试。', en: 'Upload failed. Please try again.' },
  'upload.timeout': {
    zh: '处理超时，请点击“返回”重试。',
    en: 'Processing timed out. Click “Back” to try again.',
  },
  'upload.unsupportedType': {
    zh: '不支持的文件类型。请选择以下扩展名的文件：{extensions}',
    en: 'Unsupported file type. Please choose a file with one of these extensions: {extensions}',
  },

  // Job mode / detection source options
  'mode.auto': { zh: '全自动', en: 'Fully automatic' },
  'mode.review': { zh: '先审核再遮盖', en: 'Review before redacting' },
  'source.llm': { zh: '仅用 LLM（默认，误报少）', en: 'LLM only (default, fewer false positives)' },
  'source.both': {
    zh: '两者都用（召回最大，误报多）',
    en: 'LLM + Guardrails (highest recall, more false positives)',
  },
  'source.guardrails': { zh: '仅用 Guardrails', en: 'Guardrails only' },

  // Job status text
  'status.processing': { zh: '处理中…', en: 'Processing…' },
  'status.detected': {
    zh: '检测完成，待审核（30分钟内未完成审核，任务将自动取消）',
    en: 'Detection complete, awaiting review (the job is cancelled automatically if not confirmed within 30 minutes)',
  },
  'status.completed': { zh: '已完成', en: 'Completed' },
  'status.failed': { zh: '失败', en: 'Failed' },

  // Detection review
  'review.title': { zh: '检测结果审核', en: 'Review Detections' },
  'review.selectAll': { zh: '全选', en: 'Select all' },
  'review.selectNone': { zh: '全不选', en: 'Select none' },
  'review.debug': { zh: 'Debug 模式', en: 'Debug mode' },
  'review.toggleAll': { zh: '全选或全不选', en: 'Select or clear all' },
  'review.selectRow': { zh: '选择 {value}', en: 'Select {value}' },
  'review.col.value': { zh: '敏感信息', en: 'Sensitive data' },
  'review.col.type': { zh: '类型', en: 'Type' },
  'review.col.context': { zh: '上下文', en: 'Context' },
  'review.col.count': { zh: '出现次数', en: 'Occurrences' },
  'review.count.one': { zh: '{count} 处', en: '{count} occurrence' },
  'review.count.other': { zh: '{count} 处', en: '{count} occurrences' },
  'review.pages.one': { zh: '第 {pages} 页', en: 'Page {pages}' },
  'review.pages.other': { zh: '第 {pages} 页', en: 'Pages {pages}' },
  'review.empty': { zh: '没有检测到敏感信息', en: 'No sensitive data detected' },
  'review.loading': { zh: '正在加载检测结果…', en: 'Loading detections…' },
  'review.loadFailed': {
    zh: '加载检测结果失败：{message}',
    en: 'Failed to load detections: {message}',
  },
  'review.loadFailedDefault': {
    zh: '获取检测结果失败，请重试。',
    en: 'Could not fetch the detections. Please try again.',
  },
  'review.submitFailedDefault': {
    zh: '提交确认失败，请重试。',
    en: 'Could not submit the confirmation. Please try again.',
  },
  'review.confirm': { zh: '确认遮盖', en: 'Confirm redaction' },
  'review.submitting': { zh: '提交中…', en: 'Submitting…' },

  // Rule management
  'rules.title': { zh: '规则管理', en: 'Rule Management' },
  'rules.col.name': { zh: '名称', en: 'Name' },
  'rules.col.pattern': { zh: '正则表达式', en: 'Regular expression' },
  'rules.col.status': { zh: '状态', en: 'Status' },
  'rules.col.type': { zh: '类型', en: 'Type' },
  'rules.col.actions': { zh: '操作', en: 'Actions' },
  'rules.enabled': { zh: '已启用', en: 'Enabled' },
  'rules.disabled': { zh: '已禁用', en: 'Disabled' },
  'rules.builtIn': { zh: '内置规则', en: 'Built-in' },
  'rules.custom': { zh: '自定义规则', en: 'Custom' },
  'rules.enable': { zh: '启用', en: 'Enable' },
  'rules.disable': { zh: '禁用', en: 'Disable' },
  'rules.delete': { zh: '删除', en: 'Delete' },
  'rules.builtInHint': { zh: '(内置规则不可删除)', en: '(built-in rules cannot be deleted)' },
  'rules.newTitle': { zh: '新建规则', en: 'New rule' },
  'rules.create': { zh: '新建', en: 'Create' },
  'rules.err.DUPLICATE_NAME': { zh: '该名称已存在', en: 'This name already exists' },
  'rules.err.INVALID_PATTERN': { zh: '正则表达式不合法', en: 'Invalid regular expression' },
  'rules.err.INVALID_LENGTH': {
    zh: '名称或正则长度不符合要求',
    en: 'The name or pattern length is out of range',
  },
  'rules.err.UNKNOWN_RULE_NAME': { zh: '未找到该规则名称', en: 'Rule name not found' },
  'rules.err.BUILT_IN_NOT_DELETABLE': {
    zh: '内置规则不可删除',
    en: 'Built-in rules cannot be deleted',
  },
  'rules.err.unknown': { zh: '操作被拒绝，原因未知', en: 'The operation was rejected for an unknown reason' },
  'rules.err.submitFailed': { zh: '提交失败，请重试。', en: 'Submission failed. Please try again.' },

  // Job status view (summary line and download)
  'job.status.processing': { zh: '处理中…', en: 'Processing…' },
  'job.status.detected': { zh: '检测完成，待审核', en: 'Detection complete, awaiting review' },
  'job.status.completed': { zh: '处理完成', en: 'Processing complete' },
  'job.status.failed': { zh: '处理失败', en: 'Processing failed' },
  'job.failure': {
    zh: '文档脱敏处理失败，请重新上传文档。',
    en: 'Redacting the document failed. Please upload it again.',
  },
  'job.download': { zh: '下载脱敏文档', en: 'Download redacted document' },
  'job.downloadUnavailable': {
    zh: '脱敏文档当前不可下载，请刷新状态后重试。',
    en: 'The redacted document cannot be downloaded right now. Refresh the status and try again.',
  },
  'job.dualPath': {
    zh: '已遮蔽 {total} 处敏感信息（文本层 {textLayer} 处，OCR {ocr} 处，两路一致 {agreed} 处）',
    en: 'Redacted {total} sensitive items (text layer {textLayer}, OCR {ocr}, found by both {agreed})',
  },
  'job.llmOnly': { zh: '其中仅大模型发现 {count} 处', en: '{count} found only by the LLM' },

  // Why a job failed. The backend stores a machine-readable `errorCode` (and
  // `errorParams`), shown through `translateJobError` as `job.error.<CODE>`;
  // see backend/agent/tools/job_status_update.py for the codes.
  'upload.failureReason': { zh: '失败原因', en: 'Reason' },
  'job.error.REVIEW_TIMEOUT': {
    zh: '审核超时，已取消。如需脱敏请重新上传。',
    en: 'Review timed out and the job was cancelled. Upload the document again to redact it.',
  },
  'job.error.STRUCTURE_VERIFICATION_FAILED': {
    zh: '脱敏后的文档未通过结构校验，未生成结果。请重试或更换文档。',
    en: 'The redacted document failed the structure check, so no result was produced. Try again or use a different document.',
  },
  'job.error.OCR_PAGE_FAILED': {
    zh: '第 {page} 页 OCR 识别失败，未生成结果。',
    en: 'OCR failed on page {page}, so no result was produced.',
  },
  'job.error.OCR_FAILED': {
    zh: '该 PDF 的 OCR 识别失败，未生成结果。',
    en: 'OCR failed for this PDF, so no result was produced.',
  },
  'job.error.STORAGE_WRITE_FAILED': {
    zh: '保存上传的文档失败，请重试。',
    en: 'The uploaded document could not be stored. Please try again.',
  },
  'job.error.PROCESSING_FAILED': {
    zh: '文档处理失败，请重试；如果问题持续，请联系管理员。',
    en: 'Processing the document failed. Please try again, and contact the administrator if it keeps failing.',
  },
} as const satisfies Record<string, Record<Lang, string>>

export type MessageKey = keyof typeof DICT

/** The full dictionary (read-only), e.g. for completeness checks. */
export const MESSAGES: Readonly<Record<MessageKey, Record<Lang, string>>> = DICT

export type TranslateVars = Record<string, string | number>
export type TranslateFn = (key: MessageKey, vars?: TranslateVars) => string

function translate(lang: Lang, key: MessageKey, vars?: TranslateVars): string {
  let text: string = DICT[key][lang]
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{${name}}`).join(String(value))
    }
  }
  return text
}

/**
 * Text for a job's `errorCode` in the current language, or `null` when there
 * is no code or this UI does not know it (e.g. a newer backend). The caller
 * then falls back to whatever else it has.
 */
export function translateJobError(
  t: TranslateFn,
  code: string | null | undefined,
  params?: TranslateVars | null,
): string | null {
  if (!code) return null
  const key = `job.error.${code}`
  return key in DICT ? t(key as MessageKey, params ?? undefined) : null
}

interface I18nContextValue {
  lang: Lang
  setLang: (lang: Lang) => void
  t: TranslateFn
}

const FALLBACK: I18nContextValue = {
  lang: DEFAULT_LANG,
  setLang: () => {},
  t: (key, vars) => translate(DEFAULT_LANG, key, vars),
}

const Ctx = createContext<I18nContextValue>(FALLBACK)

function readStoredLang(): Lang {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'zh' || saved === 'en') return saved
  } catch {
    /* storage unavailable (private mode, SSR): use the default */
  }
  return DEFAULT_LANG
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readStoredLang)

  const setLang = useCallback((next: Lang) => {
    setLangState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* ignore */
    }
  }, [])

  const t = useCallback<TranslateFn>((key, vars) => translate(lang, key, vars), [lang])

  // Keep the browser tab title and the <html lang> attribute in sync.
  useEffect(() => {
    document.title = DICT['app.title'][lang]
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
  }, [lang])

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useI18n(): I18nContextValue {
  return useContext(Ctx)
}
