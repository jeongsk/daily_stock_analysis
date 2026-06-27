import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import { Check, Languages } from 'lucide-react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { cn } from '../../utils/cn';

type UiLanguage = 'en' | 'zh' | 'ko';
type UiLanguageToggleVariant = 'default' | 'nav' | 'rail';

const LANGUAGE_OPTIONS: Array<{ value: UiLanguage; name: string }> = [
  { value: 'zh', name: '中文' },
  { value: 'en', name: 'English' },
  { value: 'ko', name: '한국어' },
];

function resolveLanguageName(language: UiLanguage): string {
  const option = LANGUAGE_OPTIONS.find((o) => o.value === language);
  return option?.name ?? 'English';
}

interface UiLanguageToggleProps {
  variant?: UiLanguageToggleVariant;
  collapsed?: boolean;
  wrapperClassName?: string;
  triggerClassName?: string;
  triggerActiveClassName?: string;
  iconClassName?: string;
  labelClassName?: string;
}

export const UiLanguageToggle: React.FC<UiLanguageToggleProps> = ({
  variant = 'default',
  collapsed = false,
  wrapperClassName,
  triggerClassName,
  triggerActiveClassName,
  iconClassName,
  labelClassName,
}) => {
  const { language, setLanguage, t } = useUiLanguage();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    const handlePointerDown = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };

    document.addEventListener('mousedown', handlePointerDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
    };
  }, [open]);

  const currentLanguage = language as UiLanguage;
  const isNavVariant = variant === 'nav';
  const isRailVariant = variant === 'rail';

  return (
    <div className={cn('relative', isRailVariant ? 'w-full' : '', wrapperClassName)} ref={containerRef}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        data-state={open ? 'open' : 'closed'}
        className={cn(
          triggerClassName
            ? triggerClassName
            : isRailVariant
              ? 'flex h-[var(--nav-item-height)] w-full items-center justify-center gap-2.5 rounded-2xl border border-transparent px-2 text-sm leading-none text-secondary-text transition-all hover:bg-[var(--nav-hover-bg)] hover:text-foreground data-[state=open]:border-[var(--nav-active-border)] data-[state=open]:bg-[var(--nav-active-bg)] data-[state=open]:text-[hsl(var(--primary))]'
              : isNavVariant
                ? 'group relative flex h-12 w-full select-none items-center gap-3 rounded-[1.35rem] border border-transparent px-4 text-sm text-secondary-text transition-all duration-300 hover:bg-hover hover:text-foreground data-[state=open]:border-subtle data-[state=open]:bg-subtle data-[state=open]:text-foreground'
                : 'inline-flex h-10 items-center gap-2 rounded-xl border border-border/70 bg-card/80 px-3 text-sm text-secondary-text shadow-soft-card transition-colors hover:bg-hover hover:text-foreground',
          triggerClassName && open ? triggerActiveClassName : '',
          isNavVariant && collapsed ? 'justify-center px-2' : ''
        )}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('language.toggle')}
      >
        <Languages className={iconClassName ?? cn('shrink-0', isRailVariant ? 'h-[18px] w-[18px]' : isNavVariant ? 'h-5 w-5' : 'h-4 w-4')} />
        {isRailVariant ? (
          <span className={labelClassName}>{t('language.uiLanguage')}</span>
        ) : isNavVariant ? (
          collapsed ? null : <span className="truncate text-[1.02rem] font-medium">{t('language.uiLanguage')}</span>
        ) : (
          <span className="hidden sm:inline">{resolveLanguageName(currentLanguage)}</span>
        )}
      </button>

      {open ? (
        <div
          role="menu"
          aria-label={t('language.uiLanguage')}
          className={cn(
            'z-[100] min-w-[8rem] overflow-hidden rounded-2xl border border-border/70 bg-elevated p-1.5 shadow-[0_24px_48px_rgba(3,8,20,0.32)] backdrop-blur-xl',
            isNavVariant || isRailVariant
              ? 'absolute bottom-full left-0 mb-2 w-max min-w-[9rem]'
              : 'absolute right-0 mt-2'
          )}
        >
          {LANGUAGE_OPTIONS.map(({ value, name }) => {
            const isActive = currentLanguage === value;
            return (
              <button
                key={value}
                type="button"
                role="menuitemradio"
                aria-checked={isActive}
                onClick={() => {
                  setLanguage(value);
                  setOpen(false);
                }}
                className={cn(
                  'flex w-full items-center justify-between rounded-xl px-3 py-2 text-sm transition-colors',
                  isActive
                    ? 'bg-cyan/10 text-foreground'
                    : 'text-secondary-text hover:bg-hover hover:text-foreground'
                )}
              >
                <span className="flex items-center gap-2">
                  {name}
                </span>
                {isActive ? <Check className="h-4 w-4 text-cyan" /> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
};
