import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '@bias/core'
import {
  buildSearchFilterQuery,
  buildSearchFilterSuggestions,
  ensureSearchFilterCatalogLoaded,
  resetSearchFilterCatalogForTest,
} from './searchFilterCatalog.js'

test('search filter catalog preserves extension search targets', async () => {
  resetSearchFilterCatalogForTest()
  const calls = []
  const originalGet = api.get
  api.get = async (path, options = {}) => {
    calls.push({ path, options })
    return {
      filters: [
        {
          code: 'tag-prefix',
          label: 'Tag prefix',
          module_id: 'tags',
          target: 'tag',
          syntax: 'slug:<prefix>',
          description: 'Filter tags by slug prefix',
        },
      ],
    }
  }

  try {
    await ensureSearchFilterCatalogLoaded('tag')
  } finally {
    api.get = originalGet
  }

  assert.equal(calls[0].path, '/search/filters')
  assert.equal(calls[0].options.params.target, 'tag')
  assert.deepEqual(buildSearchFilterSuggestions('tag'), [
    {
      key: 'tag:slug:<prefix>',
      label: 'Tag prefix',
      syntax: 'slug:<prefix>',
      description: 'Filter tags by slug prefix',
      target: 'tag',
    },
  ])
})

test('search filter query appends syntax once', () => {
  assert.equal(buildSearchFilterQuery('support', 'slug:sup'), 'support slug:sup')
  assert.equal(buildSearchFilterQuery('support slug:sup', 'slug:sup'), 'support slug:sup')
})
