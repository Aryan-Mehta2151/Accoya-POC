import * as Dialog from '@radix-ui/react-dialog';
import {
  BookOpenText,
  Bot,
  ChevronLeft,
  LayoutDashboard,
  MailCheck,
  Menu,
  PanelLeft,
  Sparkles,
  Target,
  X,
  LogOut,
  User as UserIcon,
} from 'lucide-react';
import { useState } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import styles from './AppShell.module.css';

const NAV_ITEMS = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/opportunities', label: 'Opportunities', icon: Target },
  { to: '/outreach', label: 'Outreach', icon: MailCheck },
  { to: '/knowledge', label: 'Knowledge Base', icon: BookOpenText },
  { to: '/assistant', label: 'Assistant', icon: Bot },
];

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <NavLink className={styles.brand} to='/' aria-label='Accoya Outreach home'>
      <span className={styles.brandMark} aria-hidden='true'>
        <span />
        <span />
      </span>
      {!compact && (
        <span className={styles.brandCopy}>
          <strong>Accoya</strong>
          <span>Outreach</span>
        </span>
      )}
    </NavLink>
  );
}

function Navigation({ compact = false, onNavigate }: { compact?: boolean; onNavigate?: () => void }) {
  return (
    <nav className={styles.navigation} aria-label='Primary navigation'>
      {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          title={compact ? label : undefined}
          className={({ isActive }) => `${styles.navItem} ${isActive ? styles.active : ''}`}
        >
          <Icon aria-hidden='true' />
          {!compact && <span>{label}</span>}
        </NavLink>
      ))}
    </nav>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  return (
    <div className={styles.userMenu}>
      <button
        type='button'
        className={styles.userMenuTrigger}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-haspopup='menu'
      >
        <span className={styles.userAvatar}><UserIcon aria-hidden='true' /></span>
        <span>{user?.name || user?.email}</span>
      </button>

      {open && (
        <div className={styles.userMenuPanel} role='menu'>
          <div className={styles.userIdentity}>
            <strong>{user?.name || 'Accoya user'}</strong>
            <span>{user?.email}</span>
          </div>
          <button
            type='button'
            className={styles.signOutButton}
            onClick={handleLogout}
            role='menuitem'
          >
            <LogOut aria-hidden='true' />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function AppShell() {
  const [collapsed, setCollapsed] = useState(
    () => window.localStorage.getItem('accoya-sidebar-collapsed') === 'true',
  );
  const [mobileOpen, setMobileOpen] = useState(false);

  const toggleCollapsed = () => {
    setCollapsed((value) => {
      window.localStorage.setItem('accoya-sidebar-collapsed', String(!value));
      return !value;
    });
  };

  return (
    <div className={`${styles.shell} ${collapsed ? styles.collapsed : ''}`}>
      <aside className={styles.sidebar}>
        <div className={styles.sidebarTop}>
          <Brand compact={collapsed} />
          <button
            className={styles.collapseButton}
            type='button'
            onClick={toggleCollapsed}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? <PanelLeft aria-hidden='true' /> : <ChevronLeft aria-hidden='true' />}
          </button>
        </div>
        <Navigation compact={collapsed} />
        <div className={styles.sidebarNote}>
          <Sparkles aria-hidden='true' />
          {!collapsed && (
            <p>
              <strong>Thoughtful outreach</strong>
              Built around every opportunity.
            </p>
          )}
        </div>
      </aside>

      <header className={styles.mobileHeader}>
        <Brand />
        <button className='iconButton' type='button' onClick={() => setMobileOpen(true)} aria-label='Open menu'>
          <Menu aria-hidden='true' />
        </button>
      </header>

      <div className={styles.userMenuPosition}>
        <UserMenu />
      </div>

      <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className={styles.mobileOverlay} />
          <Dialog.Content className={styles.mobileMenu} aria-describedby={undefined}>
            <Dialog.Title className='srOnly'>Navigation</Dialog.Title>
            <div className={styles.mobileMenuTop}>
              <Brand />
              <Dialog.Close className='iconButton' aria-label='Close menu'>
                <X aria-hidden='true' />
              </Dialog.Close>
            </div>
            <Navigation onNavigate={() => setMobileOpen(false)} />
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <main className={styles.main}>
        <div className={styles.content}>
          <Outlet />
        </div>
      </main>
    </div>
  );
}
