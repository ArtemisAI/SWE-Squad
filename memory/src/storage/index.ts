/**
 * Public entry-point for the storage layer.
 *
 * Import everything storage-related from this single path:
 *
 *   import { createStorageAdapter, SupabaseAdapter } from './storage/index.js';
 *   import type { StorageAdapter, SearchResult } from './storage/index.js';
 */

export type {
  SessionRow,
  ObservationRow,
  SummaryRow,
  SearchResult,
  CreateSessionParams,
  StoreObservationParams,
  StoreSummaryParams,
  StorageAdapter,
} from './types.js';

export { SupabaseAdapter } from './supabase-adapter.js';

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

import { SupabaseAdapter } from './supabase-adapter.js';
import type { StorageAdapter } from './types.js';

/**
 * Construct a StorageAdapter from the supplied Supabase credentials.
 *
 * The returned instance uses raw fetch() with no Supabase SDK dependency,
 * matching the pattern established in src/swe_team/supabase_store.py.
 *
 * @example
 * ```typescript
 * const storage = createStorageAdapter({
 *   supabaseUrl: process.env.SUPABASE_URL!,
 *   supabaseKey: process.env.SUPABASE_ANON_KEY!,
 * });
 * ```
 */
export function createStorageAdapter(config: {
  supabaseUrl: string;
  supabaseKey: string;
}): StorageAdapter {
  return new SupabaseAdapter(config.supabaseUrl, config.supabaseKey);
}
