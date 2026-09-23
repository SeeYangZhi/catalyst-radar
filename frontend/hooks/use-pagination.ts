// Shared pagination constants. (The former client-side `usePagination` hook
// was removed — all tables paginate via TanStack server/client pagination.)
export const DEFAULT_PAGE_SIZE = 25;
export const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;
