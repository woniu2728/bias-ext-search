
import { ref, computed } from '@bias/core'
import { getTrackedDiscussionIdsFromDiscussionItems,
  getTrackedDiscussionIdsFromPostItems
} from '@bias/realtime'

function unwrapList(payload) {
  if (Array.isArray(payload?.data)) return payload.data
  if (Array.isArray(payload?.results)) return payload.results
  if (Array.isArray(payload)) return payload
  return []
}

function sourceKey(source = {}) {
  return source.routeType || source.type
}

function sourceApiType(source = {}) {
  return source.apiType || source.type || sourceKey(source)
}

function sourceResourceType(source = {}) {
  return source.resourceType || source.storeType || sourceKey(source)
}

function sourceResultsKey(source = {}) {
  return source.resultsKey || sourceKey(source)
}

function sourceTotalKey(source = {}) {
  if (source.totalKey) return source.totalKey
  const apiType = sourceApiType(source)
  return `${apiType.endsWith('s') ? apiType.slice(0, -1) : apiType}_total`
}

export function createSearchResultsResourceState({
  resourceStore,
  searchSources,
}) {
  const total = ref(0)
  const discussionTotal = ref(0)
  const postTotal = ref(0)
  const userTotal = ref(0)
  const discussionIds = ref([])
  const postIds = ref([])
  const userIds = ref([])
  const sourceIds = ref({})
  const sourceTotals = ref({})

  const discussions = computed(() => resourceStore.list('discussions', discussionIds.value))
  const posts = computed(() => resourceStore.list('posts', postIds.value))
  const users = computed(() => resourceStore.list('users', userIds.value))
  const sourceItems = computed(() => Object.fromEntries(
    searchSources.map(source => {
      const key = sourceKey(source)
      return [key, resourceStore.list(sourceResourceType(source), sourceIds.value[key] || [])]
    })
  ))
  const totalPages = computed(() => Math.max(1, Math.ceil(total.value / 20)))
  const isEmpty = computed(() => searchSources.every(source => {
    const key = sourceKey(source)
    return !(sourceItems.value[key] || []).length
  }))
  const trackedDiscussionIds = computed(() => [
    ...getTrackedDiscussionIdsFromDiscussionItems(discussions.value),
    ...getTrackedDiscussionIdsFromPostItems(posts.value),
  ])

  function applySearchResponse(data = {}) {
    total.value = data.total || 0
    const nextSourceIds = {}
    const nextSourceTotals = {}

    for (const source of searchSources) {
      const key = sourceKey(source)
      const items = unwrapList(data[sourceResultsKey(source)] || [])
      nextSourceTotals[key] = data[sourceTotalKey(source)] ?? items.length
      nextSourceIds[key] = resourceStore.upsertMany(sourceResourceType(source), items)
        .map(item => item.id)
    }

    sourceIds.value = nextSourceIds
    sourceTotals.value = nextSourceTotals
    discussionTotal.value = sourceTotals.value.discussions ?? data.discussion_total ?? unwrapList(data.discussions || []).length
    postTotal.value = sourceTotals.value.posts ?? data.post_total ?? unwrapList(data.posts || []).length
    userTotal.value = sourceTotals.value.users ?? data.user_total ?? unwrapList(data.users || []).length
    discussionIds.value = sourceIds.value.discussions || []
    postIds.value = sourceIds.value.posts || []
    userIds.value = sourceIds.value.users || []
  }

  function resetResults() {
    total.value = 0
    discussionTotal.value = 0
    postTotal.value = 0
    userTotal.value = 0
    discussionIds.value = []
    postIds.value = []
    userIds.value = []
    sourceIds.value = {}
    sourceTotals.value = {}
  }

  function buildSearchSourceSections({ normalizedQuery, searchType }) {
    return searchSources.map(source => {
      const key = sourceKey(source)
      const items = sourceItems.value[key] || []
      const totalForSource = Number(sourceTotals.value[key] || 0)
      const resultItems = typeof source.buildResultItems === 'function'
        ? source.buildResultItems(items, { query: normalizedQuery })
        : []

      return {
        ...source,
        key,
        resultItems,
        showMore: searchType === 'all' && totalForSource > items.length,
        visible: searchType === 'all' || searchType === key,
      }
    })
  }

  return {
    applySearchResponse,
    buildSearchSourceSections,
    discussionIds,
    discussionTotal,
    discussions,
    isEmpty,
    postIds,
    postTotal,
    posts,
    resetResults,
    sourceIds,
    sourceItems,
    sourceTotals,
    total,
    totalPages,
    trackedDiscussionIds,
    userIds,
    userTotal,
    users,
  }
}

export function useSearchResultsResourceState({
  resourceStore,
  searchSources,
}) {
  return createSearchResultsResourceState({
    resourceStore,
    searchSources,
  })
}
