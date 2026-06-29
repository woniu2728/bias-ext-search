"""搜索扩展业务逻辑层。"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import OuterRef, Q, Subquery

from bias_core.extensions.platform import get_forum_registry
from bias_core.extensions.platform import PaginationService

try:
    import jieba
except ImportError:  # pragma: no cover - 部署安装依赖后走 jieba，开发环境可降级。
    jieba = None


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[A-Za-z0-9_]+")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
POSTGRES_FULL_TEXT_CONFIG = "simple"


def _get_forum_registry():
    return get_forum_registry()


def _get_searchable_post_type_codes() -> tuple[str, ...]:
    return _get_forum_registry().get_searchable_post_type_codes()


def _get_search_target(target: str) -> dict[str, Any]:
    normalized = str(target or "").strip()
    try:
        from bias_core.extensions.runtime import get_extension_host_service

        provider = get_extension_host_service(f"search.target.{normalized}")
    except Exception:
        provider = None
    if callable(provider):
        try:
            provider = provider()
        except Exception:
            provider = None
    if isinstance(provider, dict):
        return provider
    raise RuntimeError(f"搜索目标未注册: search.target.{normalized}")


def _get_optional_search_target(target: str) -> dict[str, Any] | None:
    try:
        return _get_search_target(target)
    except RuntimeError:
        return None


def _get_search_target_model(target: str):
    model = _get_search_target(target).get("model")
    if model is None:
        raise RuntimeError(f"搜索目标缺少模型: search.target.{target}")
    return model


def _get_discussion_first_post_model():
    provider = _get_search_target("discussion")
    return provider.get("first_post_model") or _get_search_target_model("post")


def _apply_search_target_visibility(target: str, queryset, *, user=None):
    handler = _get_search_target(target).get("apply_visibility")
    if callable(handler):
        return handler(queryset, user)
    return queryset


@dataclass
class SearchCriteria:
    user: Any = None
    filters: dict[str, Any] | None = None
    limit: int | None = None
    offset: int = 0
    sort: str = ""
    default_sort: bool = False
    query: str = ""
    resource: str = ""

    @property
    def is_fulltext(self) -> bool:
        return bool((self.filters or {}).get("q") or self.query)


@dataclass
class SearchContext:
    discussion_queryset: object | None
    post_queryset: object | None
    user_queryset: object | None
    discussion_total: int
    post_total: int
    user_total: int
    extra_querysets: dict[str, object] = field(default_factory=dict)
    extra_totals: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.discussion_total + self.post_total + self.user_total + sum(self.extra_totals.values())


def _get_runtime_search_filters(targets: tuple[str, ...] | None = None):
    try:
        from bias_core.extensions.runtime import get_runtime_search_service

        search_service = get_runtime_search_service()
    except Exception:
        return []
    if search_service is None:
        return []

    allowed_targets = set(targets or ())
    definitions = []
    try:
        drivers = search_service.get_drivers()
    except Exception:
        return []
    for driver in drivers:
        if allowed_targets and driver.target not in allowed_targets:
            continue
        definitions.extend(driver.filters)
    return definitions


def _get_available_search_filters(targets: tuple[str, ...] | None = None):
    definitions = list(_get_forum_registry().get_search_filters())
    definitions.extend(_get_runtime_search_filters(targets))
    if targets is not None:
        allowed = set(targets)
        definitions = [item for item in definitions if item.target in allowed]
    return definitions


class SearchService:
    """搜索服务。"""

    MIN_PAGE = 1
    MIN_LIMIT = 1
    MAX_LIMIT = 100

    @staticmethod
    def normalize_page(page: int) -> int:
        return PaginationService.normalize_page(page)

    @staticmethod
    def normalize_limit(limit: int) -> int:
        return PaginationService.normalize_limit(limit, default=20, max_limit=SearchService.MAX_LIMIT)

    @staticmethod
    def tokenize_query(query: str) -> List[str]:
        """
        将中英文混合查询拆成 token。

        中文优先使用 jieba；如果当前环境还没安装依赖，则回退到内置的
        CJK 二元切分，保证 API 不会因为缺少可选分词器而不可用。
        """
        normalized = (query or "").strip().lower()
        if not normalized:
            return []

        tokens = []
        raw_parts = TOKEN_RE.findall(normalized)

        for part in raw_parts:
            SearchService._append_token(tokens, part)

            if CJK_RE.fullmatch(part):
                if jieba:
                    for token in jieba.cut_for_search(part):
                        SearchService._append_token(tokens, token)
                else:
                    SearchService._append_cjk_fallback_tokens(tokens, part)

        return tokens[:16]

    @staticmethod
    def build_text_query(fields: List[str], query: str) -> Q:
        text_query = Q()

        for token in SearchService.tokenize_query(query):
            for field in fields:
                text_query |= Q(**{f"{field}__icontains": token})

        return text_query if text_query else Q(pk__in=[])

    @staticmethod
    def extract_filter_tokens(query: str, targets: tuple[str, ...] | None = None) -> tuple[str, dict[str, list]]:
        text_tokens: List[str] = []
        filters: dict[str, list] = {}
        allowed_targets = set(targets or ())

        for raw_token in (query or "").split():
            matched = False
            for definition in _get_available_search_filters(targets):
                if allowed_targets and definition.target not in allowed_targets:
                    continue
                parsed_value = definition.parser(raw_token)
                if parsed_value is None:
                    continue
                filters.setdefault(definition.target, []).append((definition, parsed_value))
                matched = True
                break

            if not matched:
                text_tokens.append(raw_token)

        return " ".join(text_tokens).strip(), filters

    @staticmethod
    def get_public_search_filters(targets: tuple[str, ...] | None = None):
        definitions = _get_available_search_filters(targets)
        return sorted(definitions, key=lambda item: (item.target, item.module_id, item.code, item.syntax))

    @staticmethod
    def build_discussion_search_query(query: str) -> Q:
        return (
            SearchService.build_text_query(['title', 'slug'], query) |
            SearchService.build_text_query(['posts__content'], query)
        )

    @staticmethod
    def should_use_postgres_full_text(query: str, vendor: str | None = None) -> bool:
        normalized = (query or "").strip()
        if not normalized:
            return False
        if CJK_RE.search(normalized):
            return False
        database_vendor = vendor or connection.vendor
        return database_vendor == "postgresql" and bool(ASCII_TOKEN_RE.search(normalized))

    @staticmethod
    def apply_discussion_search(queryset, query: str, user=None, filter_context: dict | None = None):
        text_query, parsed_filters = SearchService.extract_filter_tokens(query, targets=("discussion",))

        if text_query:
            title_match_q = SearchService.build_text_query(['title', 'slug'], text_query)
            if SearchService.should_use_postgres_full_text(text_query):
                queryset = SearchService._apply_postgres_discussion_search(queryset, text_query, user=user)
            elif SearchService.has_search_target("post"):
                searchable_post_types = _get_searchable_post_type_codes()
                post_model = _get_search_target_model("post")
                matching_posts = SearchService._matching_discussion_posts_queryset(
                    post_model,
                    text_query,
                    searchable_post_types,
                    user=user,
                )
                queryset = queryset.filter(
                    title_match_q | Q(id__in=Subquery(matching_posts.values("discussion_id")))
                ).annotate(
                    most_relevant_post_id=Subquery(matching_posts.values("id")[:1])
                )
            else:
                queryset = queryset.filter(title_match_q)

        context = {
            **dict(filter_context or {}),
            "user": user,
            "query": query,
            "text_query": text_query,
        }
        queryset = SearchService._apply_parsed_filters(
            queryset,
            parsed_filters,
            "discussion",
            context,
        )

        queryset = SearchService._apply_extension_search_mutators(
            "discussion",
            queryset,
            context,
        )
        return queryset

    @staticmethod
    def apply_post_search(queryset, query: str, user=None, filter_context: dict | None = None):
        text_query, parsed_filters = SearchService.extract_filter_tokens(query, targets=("post",))

        if text_query and SearchService.should_use_postgres_full_text(text_query):
            search_query = SearchService._postgres_search_query(text_query)
            search_vector = SearchVector('content', config=POSTGRES_FULL_TEXT_CONFIG)
            queryset = queryset.annotate(
                search_vector=search_vector,
                search_rank=SearchRank(search_vector, search_query),
            ).filter(search_vector=search_query)
        elif text_query:
            queryset = queryset.filter(SearchService.build_text_query(['content'], text_query))

        context = {
            **dict(filter_context or {}),
            "user": user,
            "query": query,
            "text_query": text_query,
        }
        queryset = SearchService._apply_parsed_filters(
            queryset,
            parsed_filters,
            "post",
            context,
        )

        queryset = SearchService._apply_extension_search_mutators(
            "post",
            queryset,
            context,
        )
        return queryset

    @staticmethod
    def apply_user_search(queryset, query: str):
        if SearchService.should_use_postgres_full_text(query):
            search_query = SearchService._postgres_search_query(query)
            search_vector = (
                SearchVector('username', weight='A', config=POSTGRES_FULL_TEXT_CONFIG)
                + SearchVector('display_name', weight='B', config=POSTGRES_FULL_TEXT_CONFIG)
                + SearchVector('bio', weight='C', config=POSTGRES_FULL_TEXT_CONFIG)
            )
            return queryset.annotate(
                search_vector=search_vector,
                search_rank=SearchRank(search_vector, search_query),
            ).filter(search_vector=search_query)

        queryset = queryset.filter(SearchService.build_text_query(['username', 'display_name', 'bio'], query))
        return SearchService._apply_extension_search_mutators(
            "user",
            queryset,
            {"query": query, "text_query": query},
        )

    @staticmethod
    def _apply_parsed_filters(queryset, parsed_filters: dict[str, list], target: str, context: dict):
        grouped_filters = []
        grouped_by_definition = {}

        for definition, parsed_value in parsed_filters.get(target, []):
            definition_key = id(definition)
            group = grouped_by_definition.get(definition_key)
            if group is None:
                group = [definition, []]
                grouped_by_definition[definition_key] = group
                grouped_filters.append(group)
            group[1].append(parsed_value)

        output = queryset
        for definition, values in grouped_filters:
            value = values[0] if len(values) == 1 else list(values)
            output = definition.applier(output, value, context)
        return output

    @staticmethod
    def _apply_extension_search_mutators(target: str, queryset, context: dict):
        try:
            from bias_core.extensions.runtime import get_runtime_search_service

            search_service = get_runtime_search_service()
            if search_service is None:
                return queryset
            return search_service.apply_mutators(target, queryset, context)
        except Exception:
            return queryset

    @staticmethod
    def build_search_context(query: str, user=None, include_users: bool = True) -> SearchContext:
        filter_context = {"user": user, "query": query, "_tag_slug_ids_cache": {}}
        discussion_queryset = SearchService._discussion_queryset(query, user=user, filter_context=filter_context)
        post_queryset = SearchService._post_queryset(query, user=user, filter_context=filter_context)
        user_queryset = SearchService._user_queryset(query) if include_users else None
        extra_querysets = SearchService._extra_querysets(query, user=user, filter_context=filter_context)
        extra_totals = {
            target: queryset.count()
            for target, queryset in extra_querysets.items()
        }

        return SearchContext(
            discussion_queryset=discussion_queryset,
            post_queryset=post_queryset,
            user_queryset=user_queryset,
            discussion_total=discussion_queryset.count() if discussion_queryset is not None else 0,
            post_total=post_queryset.count() if post_queryset is not None else 0,
            user_total=user_queryset.count() if user_queryset is not None else 0,
            extra_querysets=extra_querysets,
            extra_totals=extra_totals,
        )

    @staticmethod
    def get_search_totals(query: str, user=None, include_users: bool = True) -> Dict[str, int]:
        context = SearchService.build_search_context(query, user=user, include_users=include_users)
        return {
            "discussion_total": context.discussion_total,
            "post_total": context.post_total,
            "user_total": context.user_total,
            **{
                f"{target}_total": total
                for target, total in context.extra_totals.items()
            },
            "total": context.total,
        }

    @staticmethod
    def search_all(
        query: str,
        page: int = 1,
        limit: int = 20,
        user=None,
        include_users: bool = True,
        context: SearchContext | None = None,
    ) -> Dict:
        context = context or SearchService.build_search_context(
            query,
            user=user,
            include_users=include_users,
        )

        discussions = SearchService._search_discussions_queryset(
            context.discussion_queryset,
            query,
            page=1,
            limit=min(limit, 5),
        ) if context.discussion_queryset is not None else []
        posts = SearchService._search_posts_queryset(
            context.post_queryset,
            query,
            page=1,
            limit=min(limit, 5),
        ) if context.post_queryset is not None else []
        users = (
            SearchService._search_users_queryset(context.user_queryset, limit=min(limit, 5))
            if include_users and context.user_queryset is not None
            else []
        )
        extra_results = {
            target: SearchService._search_extra_queryset(
                target,
                queryset,
                page=1,
                limit=min(limit, 5),
            )
            for target, queryset in context.extra_querysets.items()
        }

        return {
            'total': context.total,
            'page': page,
            'limit': limit,
            'type': 'all',
            'discussion_total': context.discussion_total,
            'post_total': context.post_total,
            'user_total': context.user_total,
            'discussions': discussions,
            'posts': posts,
            'users': users,
            'extra_results': extra_results,
            **{
                f"{target}_total": total
                for target, total in context.extra_totals.items()
            },
            **{
                SearchService._target_results_key(target): results
                for target, results in extra_results.items()
            },
        }

    @staticmethod
    def search_preview(
        query: str,
        *,
        limit: int = 5,
        user=None,
        include_users: bool = True,
    ) -> Dict:
        preview_limit = PaginationService.normalize_limit(limit, default=5, max_limit=5)
        discussion_queryset = SearchService._discussion_queryset(query, user=user)
        post_queryset = SearchService._post_queryset(query, user=user)
        user_queryset = SearchService._user_queryset(query) if include_users else None
        extra_querysets = SearchService._extra_querysets(query, user=user)

        discussions = (
            SearchService._search_discussions_queryset(
                discussion_queryset,
                query,
                page=1,
                limit=preview_limit,
            )
            if discussion_queryset is not None
            else []
        )
        posts = (
            SearchService._search_posts_queryset(
                post_queryset,
                query,
                page=1,
                limit=preview_limit,
            )
            if post_queryset is not None
            else []
        )
        users = (
            SearchService._search_users_queryset(user_queryset, limit=preview_limit)
            if include_users and user_queryset is not None
            else []
        )
        extra_results = {
            target: SearchService._search_extra_queryset(target, queryset, page=1, limit=preview_limit)
            for target, queryset in extra_querysets.items()
        }

        return {
            'total': len(discussions) + len(posts) + len(users) + sum(len(items) for items in extra_results.values()),
            'page': 1,
            'limit': preview_limit,
            'type': 'all',
            'discussion_total': len(discussions),
            'post_total': len(posts),
            'user_total': len(users),
            'discussions': discussions,
            'posts': posts,
            'users': users,
            'extra_results': extra_results,
            **{
                f"{target}_total": len(results)
                for target, results in extra_results.items()
            },
            **{
                SearchService._target_results_key(target): results
                for target, results in extra_results.items()
            },
            'is_preview': True,
        }

    @staticmethod
    def search_discussions(
        query: str,
        page: int = 1,
        limit: int = 20,
        user=None,
        context: SearchContext | None = None,
        preload=None,
    ) -> Tuple[List[Any], int]:
        context = context or SearchService.build_search_context(query, user=user, include_users=False)
        queryset = context.discussion_queryset
        if queryset is None:
            return [], 0
        if preload is not None:
            queryset = preload(queryset)
        discussions = SearchService._search_discussions_queryset(queryset, query, page, limit)
        return discussions, context.discussion_total

    @staticmethod
    def _search_discussions(
        query: str,
        page: int = 1,
        limit: int = 20,
        user=None,
    ) -> List[Any]:
        queryset = SearchService._discussion_queryset(query, user=user)
        if queryset is None:
            return []
        return SearchService._search_discussions_queryset(queryset, query, page, limit)

    @staticmethod
    def search_posts(
        query: str,
        page: int = 1,
        limit: int = 20,
        user=None,
        context: SearchContext | None = None,
        preload=None,
    ) -> Tuple[List[Any], int]:
        context = context or SearchService.build_search_context(query, user=user, include_users=False)
        queryset = context.post_queryset
        if queryset is None:
            return [], 0
        if preload is not None:
            queryset = preload(queryset)
        posts = SearchService._search_posts_queryset(queryset, query, page, limit)
        return posts, context.post_total

    @staticmethod
    def _search_posts(
        query: str,
        page: int = 1,
        limit: int = 20,
        user=None,
    ) -> List[Any]:
        queryset = SearchService._post_queryset(query, user=user)
        if queryset is None:
            return []
        return SearchService._search_posts_queryset(queryset, query, page, limit)

    @staticmethod
    def search_users(
        query: str,
        page: int = 1,
        limit: int = 20,
        context: SearchContext | None = None,
        preload=None,
    ) -> Tuple[List[Any], int]:
        context = context or SearchService.build_search_context(query, include_users=True)
        queryset = context.user_queryset
        if queryset is None:
            return [], 0
        if preload is not None:
            queryset = preload(queryset)
        users = SearchService._search_users_queryset(queryset, page, limit)
        return users, context.user_total

    @staticmethod
    def _search_users(
        query: str,
        page: int = 1,
        limit: int = 20,
    ) -> List[Any]:
        queryset = SearchService._user_queryset(query)
        if queryset is None:
            return []
        return SearchService._search_users_queryset(queryset, page, limit)

    @staticmethod
    def search_extra_target(
        target: str,
        query: str,
        page: int = 1,
        limit: int = 20,
        user=None,
        context: SearchContext | None = None,
        preload=None,
    ) -> Tuple[List[Any], int]:
        normalized = str(target or "").strip()
        if not normalized:
            return [], 0
        context = context or SearchService.build_search_context(query, user=user, include_users=False)
        queryset = context.extra_querysets.get(normalized)
        if queryset is None:
            queryset = SearchService._extra_queryset(normalized, query, user=user)
        if queryset is None:
            return [], 0
        total = context.extra_totals.get(normalized)
        if total is None:
            total = queryset.count()
        if preload is not None:
            queryset = preload(queryset)
        return SearchService._search_extra_queryset(normalized, queryset, page, limit), total

    @staticmethod
    def get_extra_search_targets() -> list[str]:
        try:
            from bias_core.extensions.runtime import get_runtime_search_service

            search_service = get_runtime_search_service()
        except Exception:
            return []
        if search_service is None:
            return []

        targets = []
        for driver in search_service.get_drivers():
            target = str(getattr(driver, "target", "") or "").strip()
            if not target or target in {"discussion", "post", "user"} or target in targets:
                continue
            if _get_optional_search_target(target) is not None:
                targets.append(target)
        return targets

    @staticmethod
    def get_extra_search_target_config(target: str) -> dict[str, Any]:
        return _get_optional_search_target(target) or {}

    @staticmethod
    def has_search_target(target: str) -> bool:
        provider = _get_optional_search_target(target)
        return bool(provider and provider.get("model") is not None)

    @staticmethod
    def get_builtin_search_targets(*, include_users: bool = True) -> tuple[str, ...]:
        candidates = ("discussion", "post", "user") if include_users else ("discussion", "post")
        return tuple(target for target in candidates if SearchService.has_search_target(target))

    @staticmethod
    def get_extra_search_results_key(target: str) -> str:
        return SearchService._target_results_key(target)

    @staticmethod
    def _search_users_queryset(queryset, page: int = 1, limit: int = 20) -> List[Any]:
        if queryset is None:
            return []
        if SearchService._queryset_has_annotation(queryset, "search_rank"):
            queryset = queryset.order_by('-search_rank', '-discussion_count', '-comment_count')
        else:
            queryset = queryset.order_by('-discussion_count', '-comment_count')

        page = SearchService.normalize_page(page)
        limit = SearchService.normalize_limit(limit)
        offset = (page - 1) * limit
        return list(queryset[offset:offset + limit])

    @staticmethod
    def get_search_suggestions(query: str, limit: int = 5, user=None) -> List[str]:
        suggestions = []
        limit = SearchService.normalize_limit(limit)
        discussion_queryset = SearchService._discussion_queryset(query, user=user)
        if discussion_queryset is None:
            return []
        discussions = discussion_queryset.values_list('title', flat=True)[:limit]
        suggestions.extend(discussions)
        return suggestions[:limit]

    @staticmethod
    def make_excerpt(content: str, query: str, length: int = 200) -> str:
        if not content:
            return ''

        content_lower = content.lower()
        for token in SearchService.tokenize_query(query):
            index = content_lower.find(token)
            if index != -1:
                start = max(0, index - 50)
                end = min(len(content), index + len(token) + 50)
                return ('...' if start > 0 else '') + content[start:end] + ('...' if end < len(content) else '')

        return content[:length] + '...' if len(content) > length else content

    @staticmethod
    def _discussion_queryset(query: str, user=None, filter_context: dict | None = None):
        if not SearchService.has_search_target("discussion"):
            return None
        discussion_model = _get_search_target_model("discussion")
        queryset = SearchService.apply_discussion_search(
            discussion_model.objects.all(),
            query,
            user=user,
            filter_context=filter_context,
        ).distinct()
        return _apply_search_target_visibility("discussion", queryset, user=user)

    @staticmethod
    def _post_queryset(query: str, user=None, filter_context: dict | None = None):
        if not SearchService.has_search_target("post"):
            return None
        searchable_post_types = _get_searchable_post_type_codes()
        post_model = _get_search_target_model("post")
        queryset = SearchService.apply_post_search(
            post_model.objects.filter(type__in=searchable_post_types),
            query,
            user=user,
            filter_context=filter_context,
        )
        return _apply_search_target_visibility("post", queryset, user=user)

    @staticmethod
    def _user_queryset(query: str):
        if not SearchService.has_search_target("user"):
            return None
        user_model = _get_search_target_model("user")
        return SearchService.apply_user_search(user_model.objects.all(), query)

    @staticmethod
    def _extra_querysets(query: str, user=None, filter_context: dict | None = None) -> dict[str, object]:
        return {
            target: queryset
            for target in SearchService.get_extra_search_targets()
            for queryset in (SearchService._extra_queryset(target, query, user=user, filter_context=filter_context),)
            if queryset is not None
        }

    @staticmethod
    def _extra_queryset(target: str, query: str, user=None, filter_context: dict | None = None):
        provider = _get_optional_search_target(target)
        if provider is None:
            return None
        model = provider.get("model")
        if model is None:
            return None
        queryset = model.objects.all()
        queryset = _apply_search_target_visibility(target, queryset, user=user)
        context = {
            **dict(filter_context or {}),
            "user": user,
            "query": query,
            "text_query": query,
            "target": target,
        }

        try:
            from bias_core.extensions.runtime import get_runtime_search_service

            search_service = get_runtime_search_service()
        except Exception:
            search_service = None

        if search_service is not None:
            criteria = SearchCriteria(
                user=user,
                filters={"q": query},
                query=query,
                resource=target,
            )
            result = search_service.query(model, queryset, criteria, context)
            queryset = result.results
        elif query:
            queryset = queryset.none()

        return queryset.distinct() if hasattr(queryset, "distinct") else queryset

    @staticmethod
    def _search_extra_queryset(target: str, queryset, page: int = 1, limit: int = 20) -> List[Any]:
        provider = _get_optional_search_target(target) or {}
        order_by = provider.get("order_by")
        if callable(order_by):
            queryset = order_by(queryset)
        elif order_by:
            queryset = queryset.order_by(*tuple(order_by))
        else:
            queryset = queryset.order_by("pk")

        page = SearchService.normalize_page(page)
        limit = SearchService.normalize_limit(limit)
        offset = (page - 1) * limit
        return list(queryset[offset:offset + limit])

    @staticmethod
    def _target_results_key(target: str) -> str:
        custom = (_get_optional_search_target(target) or {}).get("results_key")
        if custom:
            return str(custom)
        return f"{target}s"

    @staticmethod
    def _search_discussions_queryset(queryset, query: str, page: int, limit: int) -> List[Any]:
        queryset = queryset.select_related('user', 'last_posted_user')
        if SearchService.has_search_target("post"):
            first_post_model = _get_discussion_first_post_model()
            first_post_content = first_post_model.objects.filter(
                id=OuterRef("first_post_id"),
            ).values("content")[:1]
            queryset = queryset.annotate(
                first_post_content=Subquery(first_post_content)
            )
        if SearchService._queryset_has_annotation(queryset, "search_rank"):
            queryset = queryset.order_by('-is_sticky', '-search_rank', '-comment_count', '-view_count')
        else:
            queryset = queryset.order_by('-is_sticky', '-comment_count', '-view_count')

        page = SearchService.normalize_page(page)
        limit = SearchService.normalize_limit(limit)
        offset = (page - 1) * limit
        discussions = list(queryset[offset:offset + limit])

        for discussion in discussions:
            discussion.excerpt = SearchService.make_excerpt(
                getattr(discussion, "first_post_content", "") or "",
                query,
            )

        return discussions

    @staticmethod
    def _search_posts_queryset(queryset, query: str, page: int, limit: int) -> List[Any]:
        if queryset is None:
            return []
        queryset = queryset.select_related('user', 'discussion')
        if SearchService._queryset_has_annotation(queryset, "search_rank"):
            queryset = queryset.order_by('-search_rank', '-created_at')
        else:
            queryset = queryset.order_by('-created_at')

        page = SearchService.normalize_page(page)
        limit = SearchService.normalize_limit(limit)
        offset = (page - 1) * limit
        posts = list(queryset[offset:offset + limit])

        for post in posts:
            post.discussion_title = post.discussion.title
            post.excerpt = SearchService.make_excerpt(post.content, query)

        return posts

    @staticmethod
    def _append_token(tokens: List[str], token: str):
        token = token.strip().lower()
        if token and token not in tokens:
            tokens.append(token)

    @staticmethod
    def _append_cjk_fallback_tokens(tokens: List[str], value: str):
        if len(value) <= 2:
            pieces = list(value)
        else:
            pieces = [value[index:index + 2] for index in range(len(value) - 1)]

        for piece in pieces:
            SearchService._append_token(tokens, piece)

    @staticmethod
    def _apply_postgres_discussion_search(queryset, query: str, user=None):
        search_query = SearchService._postgres_search_query(query)
        title_vector = (
            SearchVector('title', weight='A', config=POSTGRES_FULL_TEXT_CONFIG)
            + SearchVector('slug', weight='B', config=POSTGRES_FULL_TEXT_CONFIG)
        )
        queryset = queryset.annotate(
            title_search_vector=title_vector,
            search_rank=SearchRank(title_vector, search_query),
        )
        search_filter = Q(title_search_vector=search_query)
        if SearchService.has_search_target("post"):
            searchable_post_types = _get_searchable_post_type_codes()
            post_model = _get_search_target_model("post")
            matching_posts = SearchService._matching_discussion_posts_queryset(
                post_model,
                query,
                searchable_post_types,
                postgres=True,
                user=user,
            )
            search_filter |= Q(id__in=Subquery(matching_posts.values("discussion_id")))
            queryset = queryset.annotate(
                most_relevant_post_id=Subquery(matching_posts.values("id")[:1])
            )

        return queryset.filter(search_filter)

    @staticmethod
    def _postgres_search_query(query: str) -> SearchQuery:
        return SearchQuery(query, config=POSTGRES_FULL_TEXT_CONFIG, search_type="plain")

    @staticmethod
    def _matching_discussion_posts_queryset(
        post_model,
        query: str,
        searchable_post_types,
        *,
        postgres: bool = False,
        user=None,
    ):
        queryset = post_model.objects.filter(
            discussion_id=OuterRef("pk"),
            type__in=searchable_post_types,
        )
        if postgres:
            search_query = SearchService._postgres_search_query(query)
            search_vector = SearchVector("content", config=POSTGRES_FULL_TEXT_CONFIG)
            queryset = queryset.annotate(
                post_search_vector=search_vector,
                post_search_rank=SearchRank(search_vector, search_query),
            ).filter(post_search_vector=search_query).order_by("-post_search_rank", "number")
        else:
            queryset = queryset.filter(
                SearchService.build_text_query(["content"], query),
            ).order_by("number")
        return _apply_search_target_visibility("post", queryset, user=user)

    @staticmethod
    def _queryset_has_annotation(queryset, name: str) -> bool:
        return name in getattr(getattr(queryset, "query", None), "annotations", {})
