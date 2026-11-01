import { LANGUAGES, useI18n } from '../i18n'
import type { Lang } from '../i18n'

/** Language picker (简体中文 / English) shown at the right end of the top bar. */
export function LanguageSwitch() {
  const { lang, setLang, t } = useI18n()
  return (
    <select
      className="lang-switch"
      aria-label={t('app.language')}
      value={lang}
      onChange={(event) => setLang(event.target.value as Lang)}
    >
      {LANGUAGES.map((language) => (
        <option key={language.value} value={language.value}>
          {language.label}
        </option>
      ))}
    </select>
  )
}
