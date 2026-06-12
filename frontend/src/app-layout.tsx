import { CircleStackIcon, CpuChipIcon, SignalIcon } from '@heroicons/react/20/solid'
import { Outlet, useLocation } from 'react-router-dom'
import {
  Navbar,
  NavbarItem,
  NavbarLabel,
  NavbarSection,
  NavbarSpacer,
} from './catalyst/navbar'
import {
  Sidebar,
  SidebarBody,
  SidebarHeader,
  SidebarItem,
  SidebarLabel,
  SidebarSection,
} from './catalyst/sidebar'
import { SidebarLayout } from './catalyst/sidebar-layout'

export function AppLayout() {
  const { pathname } = useLocation()

  return (
    <SidebarLayout
      navbar={
        <Navbar>
          <NavbarSpacer />
          <NavbarSection>
            <NavbarItem href="/models" current={pathname.startsWith('/models')}>
              <NavbarLabel>Models</NavbarLabel>
            </NavbarItem>
            <NavbarItem href="/status" current={pathname.startsWith('/status')}>
              <NavbarLabel>Status</NavbarLabel>
            </NavbarItem>
          </NavbarSection>
        </Navbar>
      }
      sidebar={
        <Sidebar>
          <SidebarHeader>
            <div className="flex items-center gap-2 px-2 py-1">
              <CircleStackIcon className="size-5 fill-zinc-500" />
              <span className="text-sm/6 font-semibold text-zinc-950 dark:text-white">
                modelinfod
              </span>
            </div>
          </SidebarHeader>
          <SidebarBody>
            <SidebarSection>
              <SidebarItem href="/models" current={pathname.startsWith('/models')}>
                <CpuChipIcon />
                <SidebarLabel>Models</SidebarLabel>
              </SidebarItem>
              <SidebarItem href="/status" current={pathname.startsWith('/status')}>
                <SignalIcon />
                <SidebarLabel>Status</SidebarLabel>
              </SidebarItem>
            </SidebarSection>
          </SidebarBody>
        </Sidebar>
      }
    >
      <Outlet />
    </SidebarLayout>
  )
}
