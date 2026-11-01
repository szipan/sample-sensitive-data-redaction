import { useState } from 'react'
import { apiClient } from './api/apiClient'
import { UploadView } from './views/UploadView'
import { RuleManagementView } from './views/RuleManagementView'
import { LanguageSwitch } from './components/LanguageSwitch'
import { useI18n } from './i18n'

type Page = 'document' | 'rules'

function App() {
  const [page, setPage] = useState<Page>('document')
  const { t } = useI18n()

  return (
    <div className="app-layout">
      <header className="app-header">
        <span className="app-header-title">{t('app.title')}</span>
        <ul className="app-nav">
          <li>
            <button
              className={page === 'document' ? 'active' : ''}
              onClick={() => setPage('document')}
            >
              {t('nav.document')}
            </button>
          </li>
          <li>
            <button
              className={page === 'rules' ? 'active' : ''}
              onClick={() => setPage('rules')}
            >
              {t('nav.rules')}
            </button>
          </li>
        </ul>
        <LanguageSwitch />
      </header>
      <main className="app-content">
        <div className={page === 'document' ? 'app-card app-card-narrow' : 'app-card'}>
          {page === 'document' && <UploadView apiClient={apiClient} />}
          {page === 'rules' && <RuleManagementView apiClient={apiClient} />}
        </div>
      </main>
    </div>
  )
}

export default App
