from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.core.config import settings
import requests
import json
import time
import threading


class WeChatMenuCustom(_PluginBase):
    # 插件基本信息
    plugin_name = "企业微信自定义菜单栏"
    plugin_desc = "智能时序控制，自定义企业微信应用底部菜单栏，确保启动后稳定生效不被覆盖。"
    plugin_version = "1.4.0"
    plugin_icon = "wechat.png"
    plugin_author = "Final Optimized"
    plugin_config_prefix = "wechatmenucustom_"
    plugin_order = 99
    auth_level = 1

    # ---------- 配置项 ----------
    _enabled: bool = False
    _auto_apply_on_start: bool = False
    _auto_apply_on_save: bool = False
    _detect_interval: int = 5
    _max_wait_time: int = 120
    _corpid: str = ""
    _corpsecret: str = ""
    _agentid: str = ""
    _menu_config: str = ""

    # ---------- 运行时状态 ----------
    _access_token: Optional[str] = None
    _token_expires_at: float = 0
    _has_run_startup_apply: bool = False
    _detect_thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _thread_lock = threading.Lock()

    # ---------- 错误码映射 ----------
    _ERR_CODE_MAP = {
        40014: "不合法的 access_token，请检查企业ID和Secret",
        40054: "不合法的子菜单按钮类型",
        40055: "不合法的按钮类型",
        40063: "参数为空，请检查必填字段",
        40071: "不合法的应用 ID",
        41001: "缺少 access_token 参数",
        41002: "缺少 corpid 参数",
        41003: "缺少 corpsecret 参数",
        42001: "access_token 已过期，请重试",
        48002: "API 接口无权限，请检查应用权限",
        60011: "指定的成员无权限访问该应用",
        80001: "可信域名未校验通过",
    }

    # ==============================
    # 插件生命周期
    # ==============================

    def init_plugin(self, config: dict = None):
        """插件初始化入口"""
        is_first_init = not self._has_run_startup_apply

        if config:
            self._enabled = config.get("enabled", False)
            self._auto_apply_on_start = config.get("auto_apply_on_start", False)
            self._auto_apply_on_save = config.get("auto_apply_on_save", False)
            self._detect_interval = int(config.get("detect_interval", 5))
            self._max_wait_time = int(config.get("max_wait_time", 120))
            self._menu_config = config.get("menu_config") or self._get_default_menu_json()

            # 优先插件配置，兜底全局配置
            self._corpid = config.get("corpid") or (settings.WECHAT_CORPID if hasattr(settings, 'WECHAT_CORPID') else "") or ""
            self._corpsecret = config.get("corpsecret") or (settings.WECHAT_APP_SECRET if hasattr(settings, 'WECHAT_APP_SECRET') else "") or ""
            self._agentid = config.get("agentid") or str(settings.WECHAT_APP_AGENTID if hasattr(settings, 'WECHAT_APP_AGENTID') else "") or ""

            if self._enabled:
                logger.info("企业微信自定义菜单栏插件已启用")
                self._self_check()

                # 1. 首次启动：开启启动自动同步 → 启动后台检测
                if is_first_init and self._auto_apply_on_start:
                    self._start_ready_detection()

                # 2. 非首次（配置保存）：开启保存自动同步 → 立即执行
                if not is_first_init and self._auto_apply_on_save:
                    logger.info("配置已更新，自动同步菜单...")
                    result = self.apply_menu()
                    if result.get("code") == 0:
                        logger.info("配置保存后自动同步成功")
                    else:
                        logger.warning(f"配置保存后自动同步失败: {result.get('msg')}")
        else:
            # 首次安装默认值
            self._corpid = settings.WECHAT_CORPID if hasattr(settings, 'WECHAT_CORPID') else ""
            self._corpsecret = settings.WECHAT_APP_SECRET if hasattr(settings, 'WECHAT_APP_SECRET') else ""
            self._agentid = str(settings.WECHAT_APP_AGENTID if hasattr(settings, 'WECHAT_APP_AGENTID') else "")
            self._menu_config = self._get_default_menu_json()

    def stop_service(self):
        """停止插件：清理所有后台资源"""
        self._stop_event.set()

        with self._thread_lock:
            if self._detect_thread and self._detect_thread.is_alive():
                self._detect_thread.join(timeout=2)

        self._access_token = None
        self._token_expires_at = 0
        logger.info("企业微信自定义菜单栏插件已停止，后台任务已清理")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """对外 API 接口"""
        return [
            {"path": "/apply", "method": "POST", "endpoint": self.apply_menu, "auth": True},
            {"path": "/current", "method": "GET", "endpoint": self.get_current_menu, "auth": True},
            {"path": "/delete", "method": "POST", "endpoint": self.delete_menu, "auth": True},
            {"path": "/restore_official", "method": "POST", "endpoint": self.restore_official_menu, "auth": True},
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置表单"""
        return [
            {
                'component': 'VForm',
                'content': [
                    # 基础开关组
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'enabled', 'label': '启用插件'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'auto_apply_on_save', 'label': '保存配置时自动应用'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'auto_apply_on_start', 'label': '启动后自动同步菜单（智能检测就绪后执行）'}
                                }]
                            }
                        ]
                    },
                    # 检测参数
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'detect_interval',
                                        'label': '检测间隔(秒)',
                                        'type': 'number',
                                        'hint': '服务就绪检测的轮询间隔，建议 3-10 秒'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'max_wait_time',
                                        'label': '最大等待超时(秒)',
                                        'type': 'number',
                                        'hint': '超时后放弃自动同步，可手动点击应用'
                                    }
                                }]
                            }
                        ]
                    },
                    # 提示信息
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'text': '若系统已在「消息通知」中配置企业微信，下方三项可全部留空，插件将自动读取全局配置。'
                                    }
                                }]
                            }
                        ]
                    },
                    # 企业微信基础配置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'corpid', 'label': '企业ID (CorpId)', 'placeholder': 'wwxxxxxxxxxxxx'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'corpsecret', 'label': '应用Secret', 'type': 'password', 'placeholder': '留空使用全局配置'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'agentid', 'label': '应用AgentId', 'placeholder': '1000001'}
                                }]
                            }
                        ]
                    },
                    # 菜单JSON配置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VTextarea',
                                    'props': {
                                        'model': 'menu_config',
                                        'label': '菜单JSON配置',
                                        'rows': 14,
                                        'class': 'font-mono text-sm',
                                        'placeholder': '企业微信菜单JSON结构，默认已填充官方标准菜单'
                                    }
                                }]
                            }
                        ]
                    },
                    # 操作按钮
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'VBtn',
                                    'props': {'color': 'primary', 'block': True, 'api': '/apply', 'method': 'POST'},
                                    'content': [{'text': '应用菜单'}]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'VBtn',
                                    'props': {'color': 'secondary', 'block': True, 'api': '/current', 'method': 'GET'},
                                    'content': [{'text': '获取当前菜单'}]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'VBtn',
                                    'props': {'color': 'success', 'block': True, 'api': '/restore_official', 'method': 'POST'},
                                    'content': [{'text': '恢复官方菜单'}]
                                }]
                            }
                        ]
                    },
                    # 规则说明
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'warning',
                                        'variant': 'tonal',
                                        'text': '一级菜单最多3个（名称≤4个汉字），二级菜单最多5个（名称≤7个汉字）。开启「启动后自动同步」可智能等待系统初始化完成，确保自定义菜单稳定生效不被覆盖。'
                                    }
                                }]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "auto_apply_on_start": False,
            "auto_apply_on_save": False,
            "detect_interval": 5,
            "max_wait_time": 120,
            "corpid": "",
            "corpsecret": "",
            "agentid": "",
            "menu_config": _get_default_menu_json()
        }

    def get_page(self) -> List[dict]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    # ==============================
    # 智能就绪检测
    # ==============================

    def _start_ready_detection(self):
        """启动后台就绪检测线程"""
        with self._thread_lock:
            if self._detect_thread and self._detect_thread.is_alive():
                logger.debug("就绪检测线程已在运行，跳过重复创建")
                return

            self._stop_event.clear()
            self._detect_thread = threading.Thread(target=self._run_ready_detect, daemon=True)
            self._detect_thread.start()
            logger.info(f"已启动企业微信服务就绪检测，间隔 {self._detect_interval} 秒，最大等待 {self._max_wait_time} 秒")

    def _run_ready_detect(self):
        """后台线程：循环检测企业微信服务是否就绪"""
        start_time = time.time()
        try:
            while not self._stop_event.is_set():
                elapsed = time.time() - start_time
                if elapsed >= self._max_wait_time:
                    logger.warning(f"企业微信服务就绪检测超时（{self._max_wait_time}秒），放弃启动自动同步")
                    return

                try:
                    # 核心检测：尝试获取 access_token，成功则认为服务已就绪
                    self._get_access_token()
                    logger.info(f"企业微信服务已就绪（耗时 {round(elapsed, 1)} 秒），开始同步自定义菜单")
                    result = self.apply_menu()
                    if result.get("code") == 0:
                        logger.info("启动自动同步菜单成功")
                    else:
                        logger.warning(f"启动自动同步菜单失败: {result.get('msg')}")
                    return
                except Exception as e:
                    logger.debug(f"企业微信服务暂未就绪: {str(e)}，等待下次检测...")

                # 等待下一轮检测
                self._stop_event.wait(self._detect_interval)
        except Exception as e:
            logger.error(f"就绪检测线程异常: {str(e)}")
        finally:
            self._has_run_startup_apply = True

    # ==============================
    # 核心业务方法
    # ==============================

    def apply_menu(self) -> dict:
        """应用菜单到企业微信（带重试）"""
        if not self._enabled:
            return {"code": 1, "msg": "插件未启用"}
        if not self._agentid:
            return {"code": 1, "msg": "缺少 AgentId 配置，请检查企业微信设置"}

        try:
            menu_data = self._validate_menu_json(self._menu_config)
        except ValueError as e:
            logger.error(f"菜单配置校验失败: {str(e)}")
            return {"code": 1, "msg": str(e)}

        # 带重试执行
        max_retry = 2
        for attempt in range(max_retry + 1):
            try:
                return self._do_apply_menu(menu_data)
            except Exception as e:
                if attempt < max_retry:
                    logger.warning(f"应用菜单第 {attempt+1} 次失败，3秒后重试: {str(e)}")
                    time.sleep(3)
                else:
                    logger.error(f"应用菜单最终失败（已重试 {max_retry} 次）: {str(e)}")
                    return {"code": 1, "msg": f"操作失败: {str(e)}"}

    def _do_apply_menu(self, menu_data: Dict) -> dict:
        """单次执行菜单创建接口"""
        access_token = self._get_access_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/menu/create"
        params = {"access_token": access_token, "agentid": self._agentid}

        resp = requests.post(url, params=params, json=menu_data, timeout=10)
        result = resp.json()

        if result.get("errcode") == 0:
            logger.info("企业微信菜单应用成功")
            return {"code": 0, "msg": "菜单应用成功，重新进入企业微信应用即可查看"}
        else:
            err_msg = self._translate_error(result.get("errcode"), result.get("errmsg", "未知错误"))
            raise RuntimeError(err_msg)

    def get_current_menu(self) -> dict:
        """获取企业微信端当前菜单配置"""
        if not self._agentid:
            return {"code": 1, "msg": "缺少 AgentId 配置"}

        try:
            access_token = self._get_access_token()
            url = "https://qyapi.weixin.qq.com/cgi-bin/menu/get"
            params = {"access_token": access_token, "agentid": self._agentid}

            resp = requests.get(url, params=params, timeout=10)
            result = resp.json()

            if result.get("errcode") == 0 or "button" in result:
                return {
                    "code": 0,
                    "msg": "获取成功",
                    "data": json.dumps(result, ensure_ascii=False, indent=2)
                }
            else:
                return {"code": 1, "msg": self._translate_error(result.get("errcode"), result.get("errmsg", "获取失败"))}
        except Exception as e:
            return {"code": 1, "msg": f"获取失败: {str(e)}"}

    def delete_menu(self) -> dict:
        """删除当前应用菜单"""
        if not self._agentid:
            return {"code": 1, "msg": "缺少 AgentId 配置"}

        try:
            access_token = self._get_access_token()
            url = "https://qyapi.weixin.qq.com/cgi-bin/menu/delete"
            params = {"access_token": access_token, "agentid": self._agentid}

            resp = requests.get(url, params=params, timeout=10)
            result = resp.json()

            if result.get("errcode") == 0:
                logger.info("企业微信菜单已删除")
                return {"code": 0, "msg": "菜单已删除，应用底部将不再显示菜单栏"}
            else:
                return {"code": 1, "msg": self._translate_error(result.get("errcode"), result.get("errmsg", "删除失败"))}
        except Exception as e:
            return {"code": 1, "msg": f"删除失败: {str(e)}"}

    def restore_official_menu(self) -> dict:
        """一键恢复官方标准菜单"""
        official_menu = self._get_official_menu()
        self._menu_config = json.dumps(official_menu, ensure_ascii=False, indent=2)

        self.update_config({"menu_config": self._menu_config})
        logger.info("已重置为官方标准菜单配置")

        return self.apply_menu()

    # ==============================
    # 工具与底层方法
    # ==============================

    def _get_access_token(self) -> str:
        """获取 access_token（带缓存，提前5分钟刷新）"""
        if self._access_token and time.time() < self._token_expires_at - 300:
            return self._access_token

        if not self._corpid or not self._corpsecret:
            raise ValueError("企业微信配置不完整，请检查 CorpId 和 Secret")

        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        params = {"corpid": self._corpid, "corpsecret": self._corpsecret}

        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            result = resp.json()

            if result.get("errcode") == 0:
                self._access_token = result.get("access_token")
                self._token_expires_at = time.time() + result.get("expires_in", 7200)
                return self._access_token
            else:
                err_msg = self._translate_error(result.get("errcode"), result.get("errmsg", "未知错误"))
                raise ValueError(f"获取 access_token 失败: {err_msg}")
        except requests.RequestException as e:
            raise RuntimeError(f"网络请求失败: {str(e)}")

    def _translate_error(self, errcode: int, default_msg: str) -> str:
        """错误码转中文友好提示"""
        if errcode in self._ERR_CODE_MAP:
            return f"{self._ERR_CODE_MAP[errcode]}（错误码: {errcode}）"
        return f"{default_msg}（错误码: {errcode}）"

    def _validate_menu_json(self, config_str: str) -> Dict:
        """全面校验菜单JSON合法性"""
        try:
            menu_data = json.loads(config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 格式错误：第 {e.lineno} 行，{e.msg}")

        if "button" not in menu_data:
            raise ValueError("JSON 根节点必须包含 button 字段")

        buttons = menu_data["button"]
        if not isinstance(buttons, list):
            raise ValueError("button 必须是数组类型")
        if len(buttons) == 0:
            raise ValueError("至少需要配置 1 个一级菜单")
        if len(buttons) > 3:
            raise ValueError(f"一级菜单最多 3 个，当前配置了 {len(buttons)} 个")

        valid_types = {"click", "view", "scancode_push", "scancode_waitmsg",
                       "pic_sysphoto", "pic_photo_or_album", "pic_weixin",
                       "location_select", "media_id", "view_limited", "miniprogram"}

        for idx, btn in enumerate(buttons, 1):
            if "name" not in btn:
                raise ValueError(f"第 {idx} 个一级菜单缺少 name 字段")

            name = btn["name"]
            has_sub = "sub_button" in btn and btn["sub_button"] is not None
            has_type = "type" in btn

            if has_sub and has_type:
                raise ValueError(f"一级菜单「{name}」包含子菜单时不能设置 type")
            if not has_sub and not has_type:
                raise ValueError(f"一级菜单「{name}」无子菜单时必须设置 type")

            if not has_sub and has_type:
                self._validate_menu_button(btn, valid_types, f"一级菜单「{name}」")

            if has_sub:
                sub_buttons = btn["sub_button"]
                if not isinstance(sub_buttons, list):
                    raise ValueError(f"菜单「{name}」的 sub_button 必须是数组")
                if len(sub_buttons) > 5:
                    raise ValueError(f"菜单「{name}」下二级菜单最多 5 个，当前 {len(sub_buttons)} 个")

                for sub_btn in sub_buttons:
                    if "name" not in sub_btn:
                        raise ValueError(f"菜单「{name}」下的子菜单缺少 name 字段")
                    if "type" not in sub_btn:
                        raise ValueError(f"二级菜单「{sub_btn['name']}」必须设置 type 字段")
                    self._validate_menu_button(sub_btn, valid_types, f"二级菜单「{sub_btn['name']}」")

        return menu_data

    def _validate_menu_button(self, btn: Dict, valid_types: set, prefix: str):
        """校验单个菜单项必填字段"""
        btn_type = btn.get("type")
        if btn_type not in valid_types:
            raise ValueError(f"{prefix} 的 type 不合法: {btn_type}")

        if btn_type == "click" and "key" not in btn:
            raise ValueError(f"{prefix} 为 click 类型时必须设置 key 字段")
        elif btn_type == "view" and "url" not in btn:
            raise ValueError(f"{prefix} 为 view 类型时必须设置 url 字段")
        elif btn_type == "miniprogram":
            if not all(k in btn for k in ("url", "appid", "pagepath")):
                raise ValueError(f"{prefix} 为 miniprogram 类型需同时设置 url、appid、pagepath")

    def _self_check(self):
        """启动自检：配置完整性检查"""
        checks = []
        if not self._corpid:
            checks.append("缺少企业ID (CorpId)")
        if not self._corpsecret:
            checks.append("缺少应用 Secret")
        if not self._agentid:
            checks.append("缺少应用 AgentId")

        if checks:
            logger.warning(f"插件配置不完整: {'; '.join(checks)}")
        else:
            logger.info("企业微信配置校验通过")

    @staticmethod
    def _get_official_menu() -> Dict:
        """MoviePilot 官方标准菜单结构"""
        return {
            "button": [
                {"type": "click", "name": "搜索", "key": "search"},
                {
                    "name": "我的",
                    "sub_button": [
                        {"type": "click", "name": "订阅", "key": "subscribe"},
                        {"type": "click", "name": "历史", "key": "history"},
                        {"type": "click", "name": "站点", "key": "sites"}
                    ]
                },
                {"type": "view", "name": "控制台", "url": "https://moviepilot.cn"}
            ]
        }

    @staticmethod
    def _get_default_menu_json() -> str:
        """获取官方标准菜单JSON字符串"""
        return json.dumps(WeChatMenuCustom._get_official_menu(), ensure_ascii=False, indent=2)


# 静态方法外部引用（表单默认值使用）
def _get_default_menu_json() -> str:
    return WeChatMenuCustom._get_default_menu_json()
