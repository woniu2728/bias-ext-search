from datetime import timedelta
import re
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ninja.testing import TestClient
from ninja_jwt.tokens import RefreshToken

from bias_core.extensions.testing import (
    AuditLog,
    ExtensionRuntimeTestMixin,
    PaginationService,
    build_extension_test_api,
    build_extension_test_host,
    get_forum_registry,
    get_search_index_definitions,
)
from bias_core.extensions.runtime import (
    create_runtime_discussion,
    get_runtime_discussion_model,
    get_runtime_discussion_state_model,
    list_runtime_discussions,
)
from bias_core.extensions.runtime import (
    create_runtime_post,
    get_runtime_post_model,
)
from bias_ext_search.backend.services import SearchService
from bias_core.extensions.runtime import (
    get_runtime_group_model,
    get_runtime_permission_model,
    get_runtime_user_model,
)


class RuntimeModelProxy:
    def __init__(self, resolver):
        self._resolver = resolver

    def __getattr__(self, name):
        return getattr(self._resolver(), name)

    def __call__(self, *args, **kwargs):
        return self._resolver()(*args, **kwargs)


User = RuntimeModelProxy(get_runtime_user_model)
Group = RuntimeModelProxy(get_runtime_group_model)
Permission = RuntimeModelProxy(get_runtime_permission_model)


class SearchIndexDefinitionTests(ExtensionRuntimeTestMixin, TestCase):
    def test_search_extension_backend_uses_runtime_targets_not_top_level_content_imports(self):
        search_backend_dir = settings.BASE_DIR / "extensions" / "search" / "backend"
        violations = []
        pattern = re.compile(r"^(?:from|import)\s+extensions\.(?!search\b)", re.MULTILINE)
        for path in sorted(search_backend_dir.rglob("*.py")):
            if path.name == "tests.py" or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            if pattern.search(source):
                violations.append(path.relative_to(settings.BASE_DIR).as_posix())

        self.assertEqual(violations, [])

    def test_search_extension_registers_runtime_service_provider(self):
        application = self.bootstrap_extensions("search")
        service = application.get_service("search.service")

        self.assertIn("search.service", application.get_service_provider_keys(extension_id="search"))
        for key in (
            "apply_discussion_search",
            "apply_post_search",
            "apply_user_search",
            "build_search_context",
            "search_all",
            "search_preview",
            "search_discussions",
            "search_posts",
            "search_users",
            "get_public_search_filters",
            "get_search_suggestions",
            "normalize_page",
            "normalize_limit",
        ):
            self.assertTrue(callable(service[key]), key)

    def test_search_extension_boots_with_system_users_target_only(self):
        application = build_extension_test_host("search")

        self.assertIn("search.service", application.get_service_provider_keys(extension_id="search"))
        self.assertEqual(SearchService.get_builtin_search_targets(), ("user",))
        self.assertEqual(SearchService.get_builtin_search_targets(include_users=False), ())
        self.assertEqual(SearchService.get_search_totals("anything"), {
            "discussion_total": 0,
            "post_total": 0,
            "user_total": 0,
            "total": 0,
        })
        self.assertEqual(SearchService.search_discussions("anything"), ([], 0))
        self.assertEqual(SearchService.search_posts("anything"), ([], 0))
        self.assertEqual(SearchService.search_users("anything"), ([], 0))
        self.assertEqual(SearchService.get_search_suggestions("anything"), [])

    def test_search_api_handles_missing_content_targets_as_optional_capabilities(self):
        api = build_extension_test_api("search", urls_namespace="search_optional_targets")
        client = TestClient(api)

        response = client.get("/search?q=anything&type=all")
        filters_response = client.get("/search/filters")
        unavailable_response = client.get("/search?q=anything&type=posts")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["discussions"], [])
        self.assertEqual(response.json()["posts"], [])
        self.assertEqual(response.json()["users"], [])
        self.assertEqual(filters_response.status_code, 200, filters_response.content)
        self.assertEqual(filters_response.json()["filters"], [])
        self.assertEqual(unavailable_response.status_code, 400, unavailable_response.content)
        self.assertEqual(unavailable_response.json()["error"], "搜索目标不可用")

    def test_search_capabilities_are_filtered_when_extension_disabled(self):
        self.disable_extension_for_test("search")

        registry = get_forum_registry()

        self.assertFalse(any(item.module_id == "search" for item in registry.get_search_filters()))
        self.assertFalse(any(item.syntax == "author:<username>" for item in registry.get_search_filters()))

    def test_extension_detail_api_surfaces_search_frontend_route(self):
        admin = User.objects.create_superuser(
            username="search-detail-admin",
            email="search-detail-admin@example.com",
            password="password123",
        )
        token = RefreshToken.for_user(admin).access_token
        response = self.client.get(
            "/api/admin/extensions/search",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["extension"]
        self.assertEqual(payload["frontend_forum_entry"], "extensions/search/frontend/forum/index.js")
        search_routes = {
            item["name"]: item
            for item in payload["frontend_routes"]
            if item["frontend"] == "forum"
        }
        self.assertEqual(search_routes["search"]["path"], "/search")
        self.assertEqual(search_routes["search"]["component"], "./SearchResultsView.vue")
        self.assertEqual(search_routes["search"]["module_id"], "search")

    def test_extension_search_index_definitions_are_owned_by_extensions(self):
        definitions = get_search_index_definitions()
        module_by_name = {definition["name"]: definition["module_id"] for definition in definitions}

        self.assertEqual(module_by_name["discussions_title_slug_fts_idx"], "discussions")
        self.assertEqual(module_by_name["posts_content_fts_idx"], "posts")
        self.assertEqual(module_by_name["users_profile_fts_idx"], "users")


class ChineseSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="searcher",
            email="searcher@example.com",
            password="password123",
            is_email_confirmed=True,
        )

    def auth_header(self, user=None):
        token = RefreshToken.for_user(user or self.user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_chinese_query_matches_discussion_content(self):
        discussion = create_runtime_discussion(
            title="无关标题",
            content="这里讨论中文分词搜索和数据库检索体验。",
            user=self.user,
        )

        discussions, total = SearchService.search_discussions("中文搜索")

        self.assertEqual(total, 1)
        self.assertEqual(discussions[0].id, discussion.id)

    def test_discussion_list_query_uses_chinese_content_search(self):
        discussion = create_runtime_discussion(
            title="产品反馈",
            content="希望论坛原生支持中文搜索。",
            user=self.user,
        )

        discussions, total = list_runtime_discussions(q="中文搜索")

        self.assertEqual(total, 1)
        self.assertEqual(discussions[0].id, discussion.id)

    def test_discussion_search_marks_matching_reply_as_most_relevant_post(self):
        discussion = create_runtime_discussion(
            title="Unrelated title",
            content="Opening content",
            user=self.user,
        )
        reply = create_runtime_post(
            discussion_id=discussion.id,
            content="reply-specific-needle",
            user=self.user,
        )

        discussions, total = SearchService.search_discussions("reply-specific-needle", user=self.user)

        self.assertEqual(total, 1)
        self.assertEqual(discussions[0].id, discussion.id)
        self.assertEqual(discussions[0].most_relevant_post_id, reply.id)

    def test_discussion_search_leaves_title_match_for_first_post_fallback(self):
        discussion = create_runtime_discussion(
            title="Title fallback needle",
            content="Opening content",
            user=self.user,
        )

        discussions, total = SearchService.search_discussions("Title fallback needle", user=self.user)

        self.assertEqual(total, 1)
        self.assertEqual(discussions[0].id, discussion.id)
        self.assertIsNone(getattr(discussions[0], "most_relevant_post_id", None))

    def test_discussion_list_supports_registered_unanswered_sort(self):
        first_discussion = create_runtime_discussion(
            title="零回复讨论",
            content="等待回复",
            user=self.user,
        )
        answered_discussion = create_runtime_discussion(
            title="已有回复讨论",
            content="已经有回复",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=answered_discussion.id,
            content="我来回复一下",
            user=self.user,
        )

        discussions, total = list_runtime_discussions(sort="unanswered", user=self.user)

        self.assertEqual(total, 2)
        self.assertEqual(discussions[0].id, first_discussion.id)
        self.assertEqual(discussions[1].id, answered_discussion.id)

    def test_discussions_api_returns_registered_sort_catalog(self):
        create_runtime_discussion(
            title="排序目录讨论",
            content="用于测试排序元数据",
            user=self.user,
        )

        response = self.client.get("/api/discussions/", {"sort": "unanswered"})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["sort"], "unanswered")
        self.assertTrue(any(item["code"] == "latest" and item["is_default"] for item in payload["available_sorts"]))
        self.assertTrue(any(item["code"] == "unanswered" and item["toolbar_visible"] is False for item in payload["available_sorts"]))
        self.assertTrue(any(item["code"] == "newest" and item["icon"] == "fas fa-file-alt" for item in payload["available_sorts"]))

    def test_discussions_api_returns_registered_filter_catalog(self):
        create_runtime_discussion(
            title="过滤目录讨论",
            content="用于测试过滤元数据",
            user=self.user,
        )

        response = self.client.get("/api/discussions/", {"filter": "my"}, **self.auth_header())

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["filter"], "my")
        self.assertTrue(any(item["code"] == "all" and item["is_default"] for item in payload["available_filters"]))
        self.assertTrue(any(item["code"] == "my" and item["sidebar_visible"] is False for item in payload["available_filters"]))
        self.assertTrue(any(item["code"] == "unread" and item["sidebar_visible"] is False for item in payload["available_filters"]))

    def test_discussion_list_supports_registered_my_and_unread_filters(self):
        other_user = User.objects.create_user(
            username="other-filter-user",
            email="other-filter-user@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        my_discussion = create_runtime_discussion(
            title="我的讨论",
            content="我发起的主题",
            user=self.user,
        )
        unread_discussion = create_runtime_discussion(
            title="未读讨论",
            content="稍后会产生未读回复",
            user=other_user,
        )
        read_discussion = create_runtime_discussion(
            title="已读讨论",
            content="稍后会被标记已读",
            user=other_user,
        )

        DiscussionUser = get_runtime_discussion_state_model()
        DiscussionUser.objects.update_or_create(
            discussion=unread_discussion,
            user=self.user,
            defaults={"last_read_post_number": 1, "is_subscribed": False},
        )
        DiscussionUser.objects.update_or_create(
            discussion=read_discussion,
            user=self.user,
            defaults={"last_read_post_number": 1, "is_subscribed": False},
        )

        create_runtime_post(
            discussion_id=unread_discussion.id,
            content="生成未读回复",
            user=other_user,
        )

        my_discussions, my_total = list_runtime_discussions(list_filter="my", user=self.user)
        unread_discussions, unread_total = list_runtime_discussions(list_filter="unread", user=self.user)

        self.assertEqual(my_total, 1)
        self.assertEqual([item.id for item in my_discussions], [my_discussion.id])
        self.assertEqual(unread_total, 1)
        self.assertEqual([item.id for item in unread_discussions], [unread_discussion.id])

    def test_chinese_tokenizer_keeps_phrase_and_segments(self):
        tokens = SearchService.tokenize_query("中文搜索")

        self.assertIn("中文搜索", tokens)
        self.assertTrue({"中文", "搜索"}.intersection(tokens))

    def test_search_targets_are_optional_runtime_providers(self):
        with patch("bias_core.extensions.runtime.get_extension_host_service", return_value=None):
            self.assertEqual(SearchService.search_discussions("中文搜索"), ([], 0))
            self.assertEqual(SearchService.search_posts("中文搜索"), ([], 0))
            self.assertEqual(SearchService.search_users("中文搜索"), ([], 0))

    def test_postgres_full_text_is_only_used_for_latin_queries_on_postgres(self):
        self.assertTrue(SearchService.should_use_postgres_full_text("postgres search", vendor="postgresql"))
        self.assertFalse(SearchService.should_use_postgres_full_text("中文搜索", vendor="postgresql"))
        self.assertFalse(SearchService.should_use_postgres_full_text("postgres search", vendor="sqlite"))

    def test_search_api_all_returns_section_totals(self):
        create_runtime_discussion(
            title="搜索讨论标题",
            content="这里有搜索内容",
            user=self.user,
        )
        discussion = create_runtime_discussion(
            title="另一个搜索讨论",
            content="讨论里包含搜索关键字",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=discussion.id,
            content="这是一条搜索帖子内容",
            user=self.user,
        )
        User.objects.create_user(
            username="search-keyword",
            email="search-keyword@example.com",
            password="password123",
            bio="搜索用户简介",
            is_email_confirmed=True,
        )

        response = self.client.get(
            "/api/search",
            {"q": "搜索", "type": "all"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertGreaterEqual(payload["discussion_total"], 2)
        self.assertGreaterEqual(payload["post_total"], 1)
        self.assertGreaterEqual(payload["user_total"], 1)

    def test_search_api_preview_mode_returns_capped_section_results_without_full_totals(self):
        for index in range(7):
            create_runtime_discussion(
                title=f"预览标题专用词 {index}",
                content="这是一段不会命中标题预览查询的正文",
                user=self.user,
            )

        response = self.client.get(
            "/api/search",
            {"q": "预览标题专用词", "type": "all", "limit": 20, "preview": True},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["type"], "all")
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["limit"], 5)
        self.assertTrue(payload["is_preview"])
        self.assertEqual(payload["discussion_total"], 5)
        self.assertEqual(len(payload["discussions"]), 5)
        self.assertEqual(payload["discussion_total"], len(payload["discussions"]))
        self.assertEqual(payload["post_total"], len(payload["posts"]))
        self.assertEqual(payload["user_total"], len(payload["users"]))
        self.assertEqual(
            payload["total"],
            payload["discussion_total"] + payload["post_total"] + payload["user_total"],
        )


class SearchApiTests(ChineseSearchTests):
    def test_search_api_posts_type_returns_pagination_metadata(self):
        discussion = create_runtime_discussion(
            title="分页搜索讨论",
            content="讨论首帖包含分页搜索关键字",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=discussion.id,
            content="第一页搜索帖子内容",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=discussion.id,
            content="第二页搜索帖子内容",
            user=self.user,
        )

        response = self.client.get("/api/search", {"q": "搜索", "type": "posts", "page": 1, "limit": 1})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["type"], "posts")
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["limit"], 1)
        self.assertGreaterEqual(payload["total"], 2)
        self.assertGreaterEqual(payload["post_total"], 2)
        self.assertEqual(len(payload["posts"]), 1)

    def test_search_api_users_type_returns_user_totals(self):
        unique_keyword = "独有用户搜索键12345"
        matched_user = User.objects.create_user(
            username="isolated-user",
            email="search-user-only@example.com",
            password="password123",
            bio=f"这是一个{unique_keyword}",
            is_email_confirmed=True,
        )
        group = Group.objects.create(name="SearchUserGroup", color="#16a085", icon="fas fa-user-tag")
        matched_user.user_groups.add(group)

        response = self.client.get(
            "/api/search",
            {"q": unique_keyword, "type": "users"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["type"], "users")
        self.assertEqual(payload["user_total"], 1)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["users"]), 1)
        self.assertEqual(payload["users"][0]["username"], "isolated-user")
        self.assertEqual(payload["users"][0]["primary_group"]["name"], group.name)

    def test_search_api_users_type_supports_resource_field_selection(self):
        unique_keyword = "用户字段裁剪搜索键67890"
        matched_user = User.objects.create_user(
            username="isolated-user-fields",
            email="search-user-fields@example.com",
            password="password123",
            bio=f"这是一个{unique_keyword}",
            is_email_confirmed=True,
        )
        group = Group.objects.create(name="SearchUserFieldsGroup", color="#16a085", icon="fas fa-user-tag")
        matched_user.user_groups.add(group)

        response = self.client.get(
            "/api/search",
            {"q": unique_keyword, "type": "users", "fields[search_user]": "primary_group"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["users"][0]["primary_group"]["name"], group.name)
        self.assertIn("bio", payload["users"][0])

    def test_search_api_discussions_support_resource_include_for_author(self):
        keyword = "搜索讨论 include 作者"
        discussion = create_runtime_discussion(
            title=keyword,
            content="作者 include 讨论内容",
            user=self.user,
        )

        response = self.client.get(
            "/api/search",
            {"q": keyword, "type": "discussions", "fields[search_discussion]": "unknown_field", "include": "user"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual(payload["discussions"][0]["id"], discussion.id)
        self.assertIn("user", payload["discussions"][0])
        self.assertEqual(payload["discussions"][0]["user"]["username"], self.user.username)

    def test_search_api_posts_support_resource_include_for_author(self):
        keyword = "搜索回复 include 作者"
        discussion = create_runtime_discussion(
            title="搜索回复 include 讨论",
            content="首帖内容",
            user=self.user,
        )
        post = create_runtime_post(
            discussion_id=discussion.id,
            content=keyword,
            user=self.user,
        )

        response = self.client.get(
            "/api/search",
            {"q": keyword, "type": "posts", "fields[search_post]": "unknown_field", "include": "user"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 1)
        self.assertEqual(payload["posts"][0]["id"], post.id)
        self.assertIn("user", payload["posts"][0])
        self.assertEqual(payload["posts"][0]["user"]["username"], self.user.username)

    def test_search_api_user_results_avoid_n_plus_one_for_primary_group(self):
        keyword = "搜索预加载用户"
        for index in range(3):
            candidate = User.objects.create_user(
                username=f"search-preload-user-{index}",
                email=f"search-preload-user-{index}@example.com",
                password="password123",
                bio=keyword,
                is_email_confirmed=True,
            )
            group = Group.objects.create(name=f"SearchPreloadGroup{index}", color="#16a085")
            candidate.user_groups.add(group)

        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                "/api/search",
                {"q": keyword, "type": "users"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        select_group_queries = [
            query["sql"]
            for query in context.captured_queries
            if "user_groups" in query["sql"].lower()
        ]
        self.assertLessEqual(len(select_group_queries), 2)

    def test_search_api_users_type_has_bounded_query_budget(self):
        keyword = "用户搜索性能预算"
        for index in range(8):
            candidate = User.objects.create_user(
                username=f"search-query-budget-user-{index}",
                email=f"search-query-budget-user-{index}@example.com",
                password="password123",
                bio=keyword,
                is_email_confirmed=True,
            )
            group = Group.objects.create(name=f"SearchBudgetGroup{index}", color="#16a085")
            candidate.user_groups.add(group)

        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                "/api/search",
                {"q": keyword, "type": "users", "limit": 8},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["user_total"], 8)
        self.assertEqual(len(payload["users"]), 8)
        self.assertTrue(all(item["primary_group"] for item in payload["users"]))
        self.assertLessEqual(len(context.captured_queries), 10)
        self.assertLessEqual(
            sum(1 for query in context.captured_queries if "user_groups" in query["sql"].lower()),
            2,
        )

    def test_search_api_discussions_type_has_bounded_query_budget(self):
        keyword = "讨论搜索性能预算"
        for index in range(8):
            create_runtime_discussion(
                title=f"{keyword} {index}",
                content=f"首帖内容 {index}",
                user=self.user,
            )

        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                "/api/search",
                {"q": keyword, "type": "discussions", "limit": 8, "include": "user"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 8)
        self.assertEqual(len(payload["discussions"]), 8)
        self.assertTrue(all(item["user"]["username"] == self.user.username for item in payload["discussions"]))
        self.assertLessEqual(
            len(context.captured_queries),
            12,
            "\n".join(query["sql"] for query in context.captured_queries),
        )
        self.assertFalse(
            any(
                re.search(r'\bfrom\s+"?bias_ext_users_user"?\b', query["sql"], flags=re.IGNORECASE)
                and "join" not in query["sql"].lower()
                for query in context.captured_queries
            )
        )

    def test_search_api_posts_type_has_bounded_query_budget(self):
        keyword = "帖子搜索性能预算"
        discussion = create_runtime_discussion(
            title="帖子搜索预算讨论",
            content="首帖内容",
            user=self.user,
        )
        for index in range(8):
            create_runtime_post(
                discussion_id=discussion.id,
                content=f"{keyword} {index}",
                user=self.user,
            )

        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                "/api/search",
                {"q": keyword, "type": "posts", "limit": 8, "include": "user"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 8)
        self.assertEqual(len(payload["posts"]), 8)
        self.assertTrue(all(item["user"]["username"] == self.user.username for item in payload["posts"]))
        self.assertLessEqual(
            len(context.captured_queries),
            12,
            "\n".join(query["sql"] for query in context.captured_queries),
        )
        self.assertFalse(
            any(
                re.search(r'\bfrom\s+"?bias_content_discussion"?\b', query["sql"], flags=re.IGNORECASE)
                and "join" not in query["sql"].lower()
                for query in context.captured_queries
            )
        )

    def test_search_api_users_type_requires_search_permission(self):
        restricted_user = User.objects.create_user(
            username="search-no-user-permission",
            email="search-no-user-permission@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        restricted_group = Group.objects.create(name="NoUserSearch", color="#95a5a6")
        Permission.objects.create(group=restricted_group, permission="viewForum")
        restricted_user.user_groups.add(restricted_group)

        response = self.client.get(
            "/api/search",
            {"q": "搜索", "type": "users"},
            **self.auth_header(restricted_user),
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"], "没有权限搜索用户")

    def test_search_api_supports_registered_author_filter_syntax(self):
        other_user = User.objects.create_user(
            username="other-search-author",
            email="other-search-author@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        matched = create_runtime_discussion(
            title="作者过滤命中",
            content="作者过滤扩展关键字",
            user=self.user,
        )
        create_runtime_discussion(
            title="作者过滤未命中",
            content="作者过滤扩展关键字",
            user=other_user,
        )

        response = self.client.get(
            "/api/search",
            {"q": f"关键字 author:{self.user.username}", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [matched.id])

    def test_search_api_supports_registered_state_filter_syntax(self):
        sticky = create_runtime_discussion(
            title="置顶过滤讨论",
            content="置顶过滤关键字",
            user=self.user,
        )
        locked = create_runtime_discussion(
            title="锁定过滤讨论",
            content="锁定过滤关键字",
            user=self.user,
        )
        create_runtime_discussion(
            title="普通过滤讨论",
            content="过滤关键字",
            user=self.user,
        )

        sticky.is_sticky = True
        sticky.save(update_fields=["is_sticky"])
        locked.is_locked = True
        locked.save(update_fields=["is_locked"])

        sticky_response = self.client.get(
            "/api/search",
            {"q": "过滤关键字 is:sticky", "type": "discussions"},
            **self.auth_header(),
        )
        locked_response = self.client.get(
            "/api/search",
            {"q": "过滤关键字 is:locked", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(sticky_response.status_code, 200, sticky_response.content)
        self.assertEqual(locked_response.status_code, 200, locked_response.content)
        self.assertEqual([item["id"] for item in sticky_response.json()["discussions"]], [sticky.id])
        self.assertEqual([item["id"] for item in locked_response.json()["discussions"]], [locked.id])

    def test_search_api_posts_support_registered_author_filter_syntax(self):
        other_user = User.objects.create_user(
            username="other-post-search-author",
            email="other-post-search-author@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        discussion = create_runtime_discussion(
            title="帖子作者过滤讨论",
            content="首帖内容",
            user=self.user,
        )
        matched_post = create_runtime_post(
            discussion_id=discussion.id,
            content="帖子作者过滤关键字",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=discussion.id,
            content="帖子作者过滤关键字",
            user=other_user,
        )

        response = self.client.get(
            "/api/search",
            {"q": f"关键字 author:{self.user.username}", "type": "posts"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 1)
        self.assertEqual([item["id"] for item in payload["posts"]], [matched_post.id])

    def test_search_api_supports_registered_unread_filter(self):
        read_discussion = create_runtime_discussion(
            title="已读过滤讨论",
            content="关注过滤关键字",
            user=self.user,
        )
        unread_discussion = create_runtime_discussion(
            title="未读过滤讨论",
            content="关注过滤关键字",
            user=self.user,
        )

        DiscussionUser = get_runtime_discussion_state_model()
        DiscussionUser.objects.update_or_create(
            discussion=read_discussion,
            user=self.user,
            defaults={"is_subscribed": False, "last_read_post_number": read_discussion.last_post_number or 1},
        )
        DiscussionUser.objects.update_or_create(
            discussion=unread_discussion,
            user=self.user,
            defaults={"is_subscribed": False, "last_read_post_number": 0},
        )

        unread_response = self.client.get(
            "/api/search",
            {"q": "关注过滤关键字 is:unread", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(unread_response.status_code, 200, unread_response.content)
        self.assertEqual([item["id"] for item in unread_response.json()["discussions"]], [unread_discussion.id])

    def test_search_api_supports_registered_created_month_filter_for_discussions(self):
        current_month = timezone.now().strftime("%Y-%m")
        previous_month = (timezone.now() - timedelta(days=40)).strftime("%Y-%m")

        matched = create_runtime_discussion(
            title="创建月份过滤命中讨论",
            content="创建月份过滤关键字",
            user=self.user,
        )
        other = create_runtime_discussion(
            title="创建月份过滤未命中讨论",
            content="创建月份过滤关键字",
            user=self.user,
        )
        get_runtime_discussion_model().objects.filter(id=other.id).update(
            created_at=timezone.now() - timedelta(days=40),
            last_posted_at=timezone.now() - timedelta(days=40),
        )

        response = self.client.get(
            "/api/search",
            {"q": f"创建月份过滤关键字 created:{current_month}", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [matched.id])
        self.assertNotEqual(current_month, previous_month)

    def test_search_api_supports_registered_created_month_filter_for_posts(self):
        current_month = timezone.now().strftime("%Y-%m")
        discussion = create_runtime_discussion(
            title="帖子创建月份过滤讨论",
            content="首帖内容",
            user=self.user,
        )
        matched_post = create_runtime_post(
            discussion_id=discussion.id,
            content="帖子创建月份过滤关键字",
            user=self.user,
        )
        other_post = create_runtime_post(
            discussion_id=discussion.id,
            content="帖子创建月份过滤关键字",
            user=self.user,
        )
        get_runtime_post_model().objects.filter(id=other_post.id).update(
            created_at=timezone.now() - timedelta(days=40),
        )

        response = self.client.get(
            "/api/search",
            {"q": f"帖子创建月份过滤关键字 created:{current_month}", "type": "posts"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 1)
        self.assertEqual([item["id"] for item in payload["posts"]], [matched_post.id])

    def test_search_filters_api_returns_registered_filter_catalog(self):
        response = self.client.get("/api/search/filters")

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["target"], "all")
        syntaxes = {item["syntax"] for item in payload["filters"]}
        self.assertIn("author:<username>", syntaxes)
        self.assertIn("is:unread", syntaxes)
        self.assertIn("created:YYYY-MM", syntaxes)

    def test_search_filters_api_supports_target_scoping(self):
        discussions_response = self.client.get("/api/search/filters", {"target": "discussions"})
        posts_response = self.client.get("/api/search/filters", {"target": "posts"})

        self.assertEqual(discussions_response.status_code, 200, discussions_response.content)
        self.assertEqual(posts_response.status_code, 200, posts_response.content)

        discussions_payload = discussions_response.json()
        posts_payload = posts_response.json()

        self.assertEqual(discussions_payload["target"], "discussions")
        self.assertEqual(posts_payload["target"], "posts")
        self.assertTrue(all(item["target"] == "discussion" for item in discussions_payload["filters"]))
        self.assertTrue(all(item["target"] == "post" for item in posts_payload["filters"]))
        self.assertIn("created:YYYY-MM", {item["syntax"] for item in discussions_payload["filters"]})
        self.assertIn("created:YYYY-MM", {item["syntax"] for item in posts_payload["filters"]})

    def test_search_filters_api_rejects_unregistered_target(self):
        response = self.client.get("/api/search/filters", {"target": "unknown-target"})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "无效的搜索过滤目标")

    def test_search_discussions_does_not_fetch_first_post_per_result(self):
        create_runtime_discussion(
            title="搜索摘要优化一",
            content="第一条摘要内容",
            user=self.user,
        )
        create_runtime_discussion(
            title="搜索摘要优化二",
            content="第二条摘要内容",
            user=self.user,
        )

        with patch.object(get_runtime_post_model().objects, "get", side_effect=AssertionError("不应逐条 get 首帖")):
            discussions, total = SearchService.search_discussions("搜索摘要优化", user=self.user)

        self.assertEqual(total, 2)
        self.assertEqual(len(discussions), 2)
        self.assertTrue(all(discussion.excerpt for discussion in discussions))

    def test_search_discussions_uses_subquery_for_first_post_excerpt(self):
        create_runtime_discussion(
            title="子查询摘要优化一",
            content="第一条子查询摘要内容",
            user=self.user,
        )
        create_runtime_discussion(
            title="子查询摘要优化二",
            content="第二条子查询摘要内容",
            user=self.user,
        )

        with patch.object(get_runtime_post_model().objects, "in_bulk", side_effect=AssertionError("不应额外批量查询首帖")):
            discussions, total = SearchService.search_discussions("子查询摘要优化", user=self.user)

        self.assertEqual(total, 2)
        self.assertEqual(len(discussions), 2)
        self.assertTrue(all(discussion.excerpt for discussion in discussions))

    def test_search_api_normalizes_page_and_limit(self):
        for index in range(3):
            create_runtime_discussion(
                title=f"分页归一化搜索 {index}",
                content="分页归一化内容",
                user=self.user,
            )

        response = self.client.get(
            "/api/search",
            {"q": "分页归一化", "type": "discussions", "page": -5, "limit": 500},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(len(payload["discussions"]), 3)

    def test_pagination_service_normalizes_page_and_limit(self):
        page, limit = PaginationService.normalize(0, 999)

        self.assertEqual(page, 1)
        self.assertEqual(limit, 100)

    def test_search_api_all_reuses_single_search_context(self):
        create_runtime_discussion(
            title="上下文复用搜索",
            content="上下文复用内容",
            user=self.user,
        )

        with patch("bias_ext_search.backend.api.SearchService.build_search_context", wraps=SearchService.build_search_context) as build_context:
            response = self.client.get(
                "/api/search",
                {"q": "上下文复用", "type": "all"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(build_context.call_count, 1)


class SearchIndexAdminApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="search-admin",
            email="search-admin@example.com",
            password="password123",
        )

    def auth_header(self):
        token = RefreshToken.for_user(self.admin).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    @patch("bias_ext_search.backend.admin_api.SearchIndexService.rebuild_postgres_indexes")
    def test_admin_can_rebuild_search_indexes(self, rebuild_indexes):
        rebuild_indexes.return_value = {
            "message": "搜索全文索引已重建",
            "indexes": ["discussions_title_slug_fts_idx", "posts_content_fts_idx"],
            "duration_ms": 42,
        }

        response = self.client.post(
            "/api/admin/search-indexes/rebuild",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        rebuild_indexes.assert_called_once_with()
        payload = response.json()
        self.assertEqual(payload["message"], "搜索全文索引已重建")
        self.assertEqual(payload["duration_ms"], 42)
        audit_log = AuditLog.objects.get(action="admin.search_indexes.rebuild")
        self.assertEqual(audit_log.target_type, "search_index")
        self.assertEqual(audit_log.data["indexes"], ["discussions_title_slug_fts_idx", "posts_content_fts_idx"])

    @patch("bias_ext_search.backend.admin_api.SearchIndexService.rebuild_postgres_indexes")
    def test_search_index_rebuild_reports_unsupported_database(self, rebuild_indexes):
        rebuild_indexes.side_effect = RuntimeError("当前数据库不是 PostgreSQL，全文索引无需重建")

        response = self.client.post(
            "/api/admin/search-indexes/rebuild",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "当前数据库不是 PostgreSQL，全文索引无需重建")
        self.assertFalse(AuditLog.objects.filter(action="admin.search_indexes.rebuild").exists())

    @patch("bias_ext_search.backend.admin_api.QueueService.get_worker_status")
    @patch("bias_ext_search.backend.admin_api.SearchIndexService.get_status")
    def test_search_index_status_returns_runtime_snapshot(self, get_status, get_worker_status):
        get_status.return_value = {
            "supported": True,
            "status": "missing",
            "label": "缺少 1 个索引",
            "message": "建议先补齐缺失索引，再继续依赖 PostgreSQL 全文搜索。",
            "expected_indexes": ["discussions_title_slug_fts_idx", "posts_content_fts_idx"],
            "existing_indexes": ["discussions_title_slug_fts_idx"],
            "missing_indexes": ["posts_content_fts_idx"],
        }
        get_worker_status.return_value = {
            "status": "available",
            "label": "2 个 worker 在线",
            "available": True,
            "worker_count": 2,
            "message": "Celery worker 可用。",
        }
        AuditLog.objects.create(
            user=self.admin,
            action="admin.search_indexes.rebuild",
            target_type="search_index",
            data={
                "indexes": ["discussions_title_slug_fts_idx", "posts_content_fts_idx"],
                "duration_ms": 42,
            },
        )

        response = self.client.get(
            "/api/admin/search-indexes/status",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        get_status.assert_called_once_with()
        get_worker_status.assert_called_once_with()
        payload = response.json()
        self.assertTrue(payload["supported"])
        self.assertEqual(payload["status"], "missing")
        self.assertEqual(payload["existing_indexes"], ["discussions_title_slug_fts_idx"])
        self.assertEqual(payload["missing_indexes"], ["posts_content_fts_idx"])
        self.assertEqual(payload["queueWorkerLabel"], "2 个 worker 在线")
        self.assertEqual(payload["lastRebuild"]["duration_ms"], 42)
        self.assertEqual(
            payload["lastRebuild"]["indexes"],
            ["discussions_title_slug_fts_idx", "posts_content_fts_idx"],
        )

    def test_search_index_status_reports_unsupported_database_by_default(self):
        response = self.client.get(
            "/api/admin/search-indexes/status",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["supported"])
        self.assertEqual(payload["status"], "unsupported")
        self.assertIsNone(payload["lastRebuild"])
