import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppLayout } from './app-layout'
import ModelsPage from './pages/models'
import StatusPage from './pages/status'
import './styles/tailwind.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename="/modelinfod">
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<Navigate to="/models" replace />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/status" element={<StatusPage />} />
          <Route path="*" element={<Navigate to="/models" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
)
