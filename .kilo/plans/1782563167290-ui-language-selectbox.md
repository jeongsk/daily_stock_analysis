# UI 언어 선택을 드롭다운(셀렉트박스)으로 변경

## Goal
`UiLanguageToggle`을 순환 버튼(zh→ko→en)에서 **직접 언어를 고를 수 있는 드롭다운 메뉴**로 변경한다.
인접한 `ThemeToggle`이 이미 사용 중인 동일한 커스텀 드롭다운 패턴으로 통일한다.

## Approach
`apps/dsa-web/src/components/i18n/UiLanguageToggle.tsx`를 `ThemeToggle`(`apps/dsa-web/src/components/theme/ThemeToggle.tsx`)과 동일한 패턴으로 재작성:
- 트리거 버튼 + `role="menu"` 팝오버 + `role="menuitemradio"` 항목 + 활성 항목 `Check` 표시
- 외부 클릭 시 닫힘(`mousedown` 리스너), `aria-haspopup="menu"` / `aria-expanded` / `data-state`
- 3개 variant 유지: `default` / `nav` / `rail`

## Decisions
- `UiLanguageToggleProps` 인터페이스를 **그대로 유지** → 모든 호출부 변경 없음(LoginPage, ShellHeader, Shell, SidebarNav).
- 트리거 라벨은 ThemeToggle과 동일:
  - `default` → 현재 언어 네이티브 이름(中文 / English / 한국어)
  - `nav`(확장) → 제네릭 `language.uiLanguage`
  - `nav`(collapsed) → 라벨 없이 아이콘만
  - `rail` → 제네릭 `language.uiLanguage`
- 옵션 라벨은 컴포넌트 내 로컬 네이티브 이름 맵 사용(`{ zh: '中文', en: 'English', ko: '한국어' }`). 언어 선택기 표준 관행이며 `uiText.ts` 수정 불필요.
- 아이콘 `Languages`(lucide-react) 유지, aria-label은 기존 `language.toggle` 키 재사용.
- `setLanguage('zh'|'en'|'ko')`는 동기 동작 → `next-themes`의 resolvedTheme 이슈 없음.
- 데스크톱(apps/dsa-desktop): 웹 빌드를 로드하므로 변경 불필요.

## Files to edit
1. `apps/dsa-web/src/components/i18n/UiLanguageToggle.tsx`
   - ThemeToggle 구조(트리거 버튼 + 팝오버 메뉴 + outside-click)로 내부 재작성.
   - props 인터페이스, variant별 className 분기, 팝오버 위치 분기(`nav`/`rail` → `absolute bottom-full left-0`, `default` → `absolute right-0 mt-2`)는 ThemeToggle을 참조.
   - 옵션 배열: `[{ value: 'zh', name: '中文' }, { value: 'en', name: 'English' }, { value: 'ko', name: '한국어' }]`, 활성 항목에 `Check` 표시 후 메뉴 닫기.
2. `apps/dsa-web/src/contexts/__tests__/UiLanguageContext.test.tsx` (lines 105-122)
   - 순환 클릭 가정 제거 → "메뉴 열기 → English 항목 선택" 흐름으로 재작성.
   - 현재 코드의 drift(컴포넌트는 zh→ko, 테스트는 zh→en 기대)도 함께 해소.
   - 단정: 트리거 버튼(aria-label `language.toggle`), 클릭 후 메뉴 오픈(`aria-expanded=true`), `menuitemradio` 항목 클릭 → localStorage=`en`, UI가 English로 전환.

## Out of scope
- `uiText.ts` / i18n 키 변경 없음.
- 호출부(SidebarNav 등) 마크업 변경 없음.
- 데스크톱 소스 변경 없음.

## Validation
- `cd apps/dsa-web && npm run lint && npm run build`
- 재작성한 vitest 실행: `npx vitest run src/contexts/__tests__/UiLanguageContext.test.tsx`
- 사용자 가시 UI 변경 → `docs/CHANGELOG.md` `[Unreleased]` 플랫 포맷 항목 추가:
  `- [改进] UI 언어 선택을 순환 버튼에서 드롭다운(셀렉트박스)으로 변경`

## Risks / Rollback
- 위험: 낮음. 동일한 ThemeToggle 패턴 재사용, props 불변, 단일 컴포넌트 + 단일 테스트.
- 롤백: `UiLanguageToggle.tsx`와 테스트를 변경 전으로 되돌림.
