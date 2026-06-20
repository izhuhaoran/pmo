"""
Service management functionality for PMO.
"""
import os
import sys
import subprocess
import signal
import yaml
import logging
import time
import psutil
import socket
from pathlib import Path
from datetime import datetime
import re
import shutil
import mimetypes
from typing import Dict, Union, List, Optional, Any, Tuple, Mapping
from dotenv import dotenv_values

from pmo.logs import console

# 初始化mimetypes
mimetypes.init()
# 确保.py扩展名被正确映射为Python脚本
if '.py' not in mimetypes.types_map:
    mimetypes.add_type('text/x-python', '.py')
if '.pyw' not in mimetypes.types_map:
    mimetypes.add_type('text/x-python', '.pyw')

logger = logging.getLogger(__name__)

class ReadableYamlDumper(yaml.SafeDumper):
    """YAML dumper tuned for readable log output."""

    def ignore_aliases(self, data):
        return True


def _represent_readable_str(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


ReadableYamlDumper.add_representer(str, _represent_readable_str)

class ServiceManager:
    """Manages processes based on pmo.yml configuration."""
    
    def __init__(self, config_path: str = "pmo.yml", pmo_dir: str = ".pmo"):
        self.config_path = config_path
        # 修改为使用配置文件所在目录
        config_dir = os.path.dirname(os.path.abspath(config_path))
        self.config_dir = config_dir
        self.pmo_base_dir = Path(config_dir) / pmo_dir
        # 使用主机名创建子目录，使多台机器可共享同一个NAS
        hostname = socket.gethostname()
        self.pmo_dir = self.pmo_base_dir / hostname
        self.pid_dir = self.pmo_dir / "pids"
        self.log_dir = self.pmo_dir / "logs"
        # 存储服务启动时间，用于计算运行时长
        self.start_times = {}
        # 存储服务重启次数
        self.restarts = {}
        # 存储从.env文件加载的环境变量
        self.dotenv_vars = {}
        self._ensure_dirs()
        # 加载.env文件
        self._load_dotenv()
        self.services = self._load_config()
        # 加载现有的服务启动时间
        self._load_start_times()
        # 加载现有的服务重启次数
        self._load_restarts()
        
    def _ensure_dirs(self):
        """Create required directories if they don't exist."""
        self.pid_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_dotenv(self):
        """Load environment variables from .env file if it exists."""
        dotenv_path = Path(self.config_dir) / ".env"
        
        if not dotenv_path.exists():
            logger.debug(f"No .env file found at {dotenv_path}")
            return
        
        try:
            # 使用 python-dotenv 加载环境变量
            self.dotenv_vars = dotenv_values(dotenv_path)
            logger.info(f"Loaded {len(self.dotenv_vars)} environment variables from {dotenv_path}")
        except Exception as e:
            logger.error(f"Error loading .env file: {str(e)}")
    
    def _load_start_times(self):
        """加载已运行服务的启动时间"""
        for service_name in self.get_service_names():
            if self.is_running(service_name):
                # 尝试从文件获取启动时间，如果没有则使用当前时间
                start_time_file = self.pid_dir / f"{service_name}.time"
                if start_time_file.exists():
                    try:
                        with open(start_time_file, "r") as f:
                            timestamp = float(f.read().strip())
                            self.start_times[service_name] = timestamp
                    except (ValueError, IOError):
                        # 如果文件无法读取或格式不正确，使用当前时间
                        self.start_times[service_name] = time.time()
                else:
                    # 如果没有时间文件，使用当前时间
                    self.start_times[service_name] = time.time()
    
    def _load_restarts(self):
        """加载服务重启次数"""
        for service_name in self.get_service_names():
            restart_file = self.pid_dir / f"{service_name}.restarts"
            if restart_file.exists():
                try:
                    with open(restart_file, "r") as f:
                        count = int(f.read().strip())
                        self.restarts[service_name] = count
                except (ValueError, IOError):
                    # 如果文件无法读取或格式不正确，设置为0
                    self.restarts[service_name] = 0
            else:
                # 如果没有重启文件，设置为0
                self.restarts[service_name] = 0
        
    def _load_config(self) -> Dict[str, Any]:
        """Load service configurations from pmo.yml, supporting 'extends' inheritance."""
        if not os.path.exists(self.config_path):
            logger.error(f"Configuration file not found: {self.config_path}")
            return {}

        try:
            with open(self.config_path, 'r') as file:
                config = yaml.safe_load(file) or {}

            # Step 1: 规范化所有服务配置
            raw_config = {}
            for name, conf in config.items():
                if name.lower() == "pmo":
                    logger.warning(f"'pmo' is a reserved name and cannot be used as a service name.")
                    continue
                if isinstance(conf, str):
                    raw_config[name] = {"cmd": conf}
                elif isinstance(conf, dict):
                    # 允许只包含 extends 的 dict，后续递归处理
                    d = dict(conf)
                    if "script" in d:
                        d["cmd"] = d["script"]
                    raw_config[name] = d
                else:
                    logger.warning(f"Invalid configuration for service '{name}', skipping.")

            # Step 2: 递归合并 extends
            def merge_env(parent_env, child_env):
                result = dict(parent_env or {})
                result.update(child_env or {})
                return result

            def merge_service(parent, child):
                merged = dict(parent)
                merged.update(child)
                # env 字典递归合并
                if "env" in parent or "env" in child:
                    merged["env"] = merge_env(parent.get("env"), child.get("env"))
                return merged

            def resolve_extends(name, seen=None):
                if seen is None:
                    seen = set()
                if name in seen:
                    raise ValueError(f"Circular extends detected for service '{name}'")
                seen.add(name)
                conf = raw_config.get(name)
                if conf is None:
                    raise ValueError(f"Service '{name}' not found for extends")
                if "extends" in conf:
                    parent_name = conf["extends"]
                    if parent_name not in raw_config:
                        raise ValueError(f"Service '{name}' extends unknown service '{parent_name}'")
                    parent_conf = resolve_extends(parent_name, seen)
                    merged = merge_service(parent_conf, {k: v for k, v in conf.items() if k != "extends"})
                    return merged
                else:
                    return dict(conf)

            # Step 3: resolve extends
            resolved = {}
            for name in raw_config:
                try:
                    merged = resolve_extends(name)
                    if not isinstance(merged, dict):
                        logger.warning(f"Invalid configuration for service '{name}', skipping.")
                        continue
                    resolved[name] = merged
                except Exception as e:
                    logger.error(f"Error resolving extends for service '{name}': {e}")

            # Step 4: expand pipeline_sweep into sub-tasks + pipeline
            for name in list(resolved.keys()):
                conf = resolved[name]
                if "pipeline_sweep" in conf:
                    extends_name = raw_config.get(name, {}).get("extends", name)
                    sub_tasks = self._expand_sweep(name, extends_name, conf)
                    resolved.update(sub_tasks)

            # Step 5: convert pipeline -> cmd, validate
            validated_config = {}
            for name, conf in resolved.items():
                if "pipeline" in conf:
                    if "cmd" in conf:
                        logger.warning(f"Service '{name}' has both 'pipeline' and 'cmd'. "
                                       f"The existing cmd is ignored, pipeline generates its own.")
                    conf["cmd"] = self._build_pipeline_cmd(conf)
                if "cmd" not in conf:
                    logger.warning(f"Invalid configuration for service '{name}' (no cmd, pipeline, or sweep), skipping.")
                    continue
                validated_config[name] = conf

            return validated_config
        except Exception as e:
            logger.error(f"Error loading configuration: {str(e)}")
            return {}
    
    def _expand_sweep(self, name: str, extends_name: str, config: Dict) -> Dict[str, Dict]:
        """Expand a sweep task into sub-tasks. Mutates config to become a pipeline task.

        Sub-tasks are named ``_{extends_name}__{suffix}`` so the lineage is
        clear when browsing ``pmo ls``.

        Returns:
            Dict of generated sub-task name -> config.
        """
        import copy
        import itertools

        sweep_vars = config.pop("pipeline_sweep")

        # Preserve pipeline control fields
        p_sleep = config.get("pipeline_sleep", 5)
        p_flush = config.get("pipeline_flush", False)
        p_poll = config.get("pipeline_poll_interval", 10)

        # Base config for sub-tasks (strip pipeline/sweep control fields)
        ctl_keys = {"pipeline_sweep", "pipeline_sleep", "pipeline_flush", "pipeline_poll_interval", "pipeline"}
        base_config = {k: v for k, v in config.items() if k not in ctl_keys}

        # Cartesian product of sweep variables
        var_names = list(sweep_vars.keys())
        var_values = [v if isinstance(v, list) else [v] for v in sweep_vars.values()]

        sub_tasks = {}
        sub_names = []

        for combo in itertools.product(*var_values):
            suffix = "_".join(f"{var}_{val}" for var, val in zip(var_names, combo))
            sub_name = f"_{extends_name}__{suffix}"

            # Deduplicate if another sweep already generated the same name
            if sub_name in sub_tasks:
                sub_name = f"_{name}__{extends_name}__{suffix}"

            sub_config = copy.deepcopy(base_config)
            if "env" not in sub_config:
                sub_config["env"] = {}

            for var, val in zip(var_names, combo):
                sub_config["env"][var] = str(val)

            # Auto-set exp_name so log files are unique per combination
            base_exp = sub_config["env"].get("exp_name", "")
            sub_config["env"]["exp_name"] = f"{base_exp}_{suffix}" if base_exp else suffix

            sub_tasks[sub_name] = sub_config
            sub_names.append(sub_name)

        # Mutate config into a pipeline task referencing the generated sub-tasks
        log_settings = {k: config[k] for k in ("merge_logs", "log_with_timestamp") if k in config}
        config.clear()
        config["pipeline"] = sub_names
        config["pipeline_sleep"] = p_sleep
        config["pipeline_flush"] = p_flush
        config["pipeline_poll_interval"] = p_poll
        config.update(log_settings)

        logger.info(f"Expanded sweep '{name}' (extends: {extends_name}) "
                     f"into {len(sub_names)} sub-tasks: {', '.join(sub_names)}")
        return sub_tasks

    def _build_pipeline_cmd(self, config: Dict) -> str:
        """Build a ``pmo pipeline`` command string from a pipeline task config."""
        import shlex
        tasks = config["pipeline"]
        if isinstance(tasks, str):
            tasks = [t.strip() for t in tasks.split(",")]

        sleep_val = config.get("pipeline_sleep", 5)
        poll_val = config.get("pipeline_poll_interval", 10)
        flush_flag = config.get("pipeline_flush", False)

        parts = [
            sys.executable, "-m", "pmo.cli",
            "-f", os.path.abspath(self.config_path),
            "pipeline",
        ]
        parts.extend(tasks)
        parts.extend(["--sleep", str(sleep_val), "--poll-interval", str(poll_val)])
        if flush_flag:
            parts.append("--flush")
        return " ".join(shlex.quote(p) for p in parts)

    def get_pid_file(self, service_name: str) -> Path:
        """Get the path to a service's PID file."""
        return self.pid_dir / f"{service_name}.pid"
    
    def get_service_pid(self, service_name: str) -> Optional[int]:
        """Get the PID for a running service, or None if not running."""
        pid_file = self.get_pid_file(service_name)
        if not pid_file.exists():
            return None
            
        try:
            with open(pid_file, 'r') as f:
                pid = int(f.read().strip())
                
            # Check if process is still running
            if self._is_process_running(pid):
                return pid
            else:
                # Clean up stale PID file
                os.remove(pid_file)
                # 删除 .logfile 文件
                logfile_hint_path = self.log_dir / f"{service_name}.logfile"
                if logfile_hint_path.exists():
                    os.remove(logfile_hint_path)
                # 删除相关的启动时间记录
                if service_name in self.start_times:
                    del self.start_times[service_name]
                start_time_file = self.pid_dir / f"{service_name}.time"
                if start_time_file.exists():
                    os.remove(start_time_file)
                return None
        except (ValueError, FileNotFoundError):
            return None
    
    def get_uptime(self, service_name: str) -> Optional[float]:
        """获取服务运行时间（以秒为单位）"""
        if service_name in self.start_times and self.is_running(service_name):
            return time.time() - self.start_times[service_name]
        return None
    
    def format_uptime(self, uptime_seconds: Optional[float]) -> str:
        """将运行时间格式化为易读格式"""
        if uptime_seconds is None:
            return "-"
        
        # 转换为整数秒
        seconds = int(uptime_seconds)
        
        # 计算天、小时、分钟、秒
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        
        # 格式化为易读字符串
        if days > 0:
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    
    def _is_process_running(self, pid: int) -> bool:
        """Check if process with given PID is running."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't have permission to send signals to it
            return True
    
    def _active_pids(self, pids: List[int]) -> List[int]:
        """Return the PIDs that are still genuinely alive.

        Zombie/defunct processes are excluded: they are already dead and get
        reaped automatically, so PMO must never wait on (or block for) them.
        """
        alive = []
        for pid in pids:
            try:
                status = psutil.Process(pid).status()
                if status not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    alive.append(pid)
            except psutil.NoSuchProcess:
                continue  # Already gone.
            except psutil.AccessDenied:
                alive.append(pid)  # Exists but not introspectable; assume alive.
            except Exception:
                if self._is_process_running(pid):
                    alive.append(pid)
        return alive

    def _signal_tree(self, pgid: Optional[int], pids: List[int], sig: int) -> None:
        """Send `sig` to the whole process group and to each known PID."""
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                continue

    def _group_pids(self, pgid: Optional[int]) -> List[int]:
        """Return all live PIDs currently in process group `pgid`.

        Robust for multi-process tasks: a child keeps the same process group
        even after the root dies (pgid survives reparenting to init), so this
        catches descendants the one-time recursive snapshot may have missed.
        """
        if pgid is None:
            return []
        found = []
        for proc in psutil.process_iter(["pid"]):
            pid = proc.info["pid"]
            try:
                if os.getpgid(pid) == pgid:
                    found.append(pid)
            except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
                continue
        return found

    def _surviving_pids(self, pids: List[int], pgid: Optional[int]) -> List[int]:
        """Live (non-zombie) PIDs from the recorded tree AND the process group."""
        candidates = set(pids) | set(self._group_pids(pgid))
        return self._active_pids(list(candidates))

    def _ps_lines(self, pids: List[int]) -> List[str]:
        """Return `ps`-style lines (header + rows) describing the given PIDs."""
        if not pids:
            return []
        try:
            result = subprocess.run(
                ["ps", "-o", "pid,ppid,stat,command", "-p", ",".join(map(str, pids))],
                capture_output=True, text=True, timeout=5,
            )
            lines = [ln.rstrip() for ln in result.stdout.splitlines() if ln.strip()]
            if lines:
                return lines
        except Exception as e:
            logger.debug(f"ps lookup failed: {e}")
        # Fallback: best-effort via psutil if `ps` is unavailable.
        lines = ["  PID  PPID STAT COMMAND"]
        for pid in pids:
            try:
                p = psutil.Process(pid)
                with p.oneshot():
                    cmd = " ".join(p.cmdline()) or p.name()
                    lines.append(f"{pid:>5} {p.ppid():>5} {p.status():>4} {cmd}")
            except Exception:
                lines.append(f"{pid:>5}     ?    ? (details unavailable)")
        return lines
    
    def is_running(self, service_name: str) -> bool:
        """Check if a service is running."""
        return self.get_service_pid(service_name) is not None
    
    def get_service_names(self) -> List[str]:
        """Get list of configured service names."""
        return list(self.services.keys())
    
    def get_running_services(self) -> List[str]:
        """Get list of currently running services."""
        return [name for name in self.get_service_names() if self.is_running(name)]

    def _dump_service_yaml(self, service_name: str) -> str:
        """Return the effective YAML snippet for a service."""
        return yaml.dump(
            {service_name: self.services.get(service_name, {})},
            Dumper=ReadableYamlDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    @staticmethod
    def _normalize_env_vars(env_vars: Mapping[str, Any]) -> Dict[str, str]:
        """Convert env values to strings, treating YAML null as an empty value."""
        return {key: "" if value is None else str(value) for key, value in env_vars.items()}

    def _is_python_script(self, cmd: str, cwd: Optional[str] = None) -> bool:
        """
        判断命令是否运行Python脚本
        
        Args:
            cmd: 要执行的命令
            cwd: 命令的工作目录
            
        Returns:
            bool: 如果是Python脚本则返回True，否则返回False
        """
        # 检查命令是否以python解释器开头
        if cmd.strip().startswith(('python', 'python3', 'python2', '/usr/bin/python')) or \
           any(part.endswith(('python', 'python3', 'python2')) for part in cmd.split()):
            return True
            
        # 检查第一个参数是否是Python脚本文件
        cmd_parts = cmd.strip().split()
        if not cmd_parts:
            return False
            
        # 获取可能的脚本文件路径
        possible_script = cmd_parts[0]
        
        # 处理相对路径
        script_path = possible_script
        if not os.path.isabs(possible_script) and cwd:
            script_path = os.path.join(cwd, possible_script)
            
        # 检查文件是否存在
        if not os.path.isfile(script_path):
            return False
            
        # 使用mimetypes.guess_type检测文件类型
        mime_type, _ = mimetypes.guess_type(script_path)
        return mime_type == 'text/x-python'
        
    def start(self, service_name: str, dry_run: bool = False) -> bool:
        """
        Start a specified service.
        
        Args:
            service_name: 服务名称
            dry_run: 若为True，只返回将要执行的命令而不实际执行
        """
        if service_name not in self.services:
            logger.error(f"Service '{service_name}' not found in configuration.")
            return False
        if self.is_running(service_name) and not dry_run:
            logger.info(f"Service '{service_name}' is already running.")
            return True
        config = self.services[service_name]
        cmd = config.get("cmd")
        if not cmd:
            logger.error(f"No command specified for service '{service_name}'.")
            return False
        # Prepare environment variables (优先级: config.env > .env > os.environ)
        from pmo.util import substitute_env_vars
        env = dict(os.environ)
        env.update(self._normalize_env_vars(self.dotenv_vars))
        if "env" in config and isinstance(config["env"], dict):
            config_env = self._normalize_env_vars(config["env"])
            env.update(config_env)
        # 环境变量替换: 支持 ${VAR}、${VAR:-default} 语法
        cmd = substitute_env_vars(cmd, env)
        # Prepare working directory
        cwd = config.get("cwd", None)
        # 使用纯Python方式检测是否为Python脚本
        if self._is_python_script(cmd, cwd):
            env['PYTHONUNBUFFERED'] = '1'
            logger.debug(f"Auto-enabled unbuffered mode for Python process: {service_name}")
            
        if dry_run:
            # 构造将要执行的命令字符串，但不执行
            cmd_str = ""
            
            # 如果指定了工作目录，添加cd命令
            if cwd:
                cmd_str += f"cd {cwd} && "
            
            # 添加环境变量
            if env:
                env_str = " ".join([f"{key}={value}" for key, value in env.items()])
                if env_str:
                    cmd_str += f"{env_str} "
            
            # 添加实际命令和参数
            cmd_str += cmd
            
            # 打印命令但不执行
            console.print(f"[bold cyan]{service_name}[/]: {cmd_str}")
            return True
        
        # 非dry-run模式下的实际执行代码
        env_copy = os.environ.copy()
        if env:
            env_copy.update(env)

        # 检查是否启用日志合并, 时间戳和日志备份
        merge_logs = config.get('merge_logs', False)
        log_with_timestamp = config.get('log_with_timestamp', False)
        log_backup = config.get('log_backup', False)

        # Prepare log files
        if merge_logs:
            # 合并日志模式
            if log_with_timestamp:
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                log_filename = f"{service_name}_{timestamp}.log"
            else:
                log_filename = f"{service_name}.log"

            stdout_log = self.log_dir / log_filename
            stderr_log = stdout_log

            if not log_with_timestamp and log_backup:
                self._backup_log_file(stdout_log)
        else:
            # 分离日志模式
            if log_with_timestamp:
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                stdout_log = self.log_dir / f"{service_name}-out_{timestamp}.log"
                stderr_log = self.log_dir / f"{service_name}-error_{timestamp}.log"
            else:
                stdout_log = self.log_dir / f"{service_name}-out.log"
                stderr_log = self.log_dir / f"{service_name}-error.log"

            if not log_with_timestamp and log_backup:
                self._backup_log_file(stdout_log)
                self._backup_log_file(stderr_log)

        # Startup details (one path per line so each stays click-to-open)
        logger.info(
            f"Service '{service_name}': merge_logs={merge_logs}\n"
            f"log_dir: {self.log_dir}\n"
            f"stdout_log: {stdout_log}\n"
            f"stderr_log: {stderr_log}"
        )
        
        try:
            with open(stdout_log, 'a') as out, open(stderr_log, 'a') as err:
                # Add timestamp to logs
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                service_yaml = self._dump_service_yaml(service_name)
                if merge_logs:
                    # 合并模式：只写一次启动信息
                    out.write(f"\n--- Starting service '{service_name}' at {timestamp} (merged logs) ---\n")
                    out.write(service_yaml)
                else:
                    # 分离模式：分别写入启动信息
                    out.write(f"\n--- Starting service '{service_name}' at {timestamp} ---\n")
                    err.write(f"\n--- Starting service '{service_name}' at {timestamp} ---\n")
                    out.write(service_yaml)
                    err.write(service_yaml)
                
                # Start the process
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=out,
                    stderr=err,
                    cwd=cwd,
                    env=env_copy,
                    start_new_session=True  # Detach from current process group
                )

            # 写入 .logfile 文件，内容为每个日志文件一行（合并模式一个，分离模式两个）
            logfile_hint_path = self.log_dir / f"{service_name}.logfile"
            with open(logfile_hint_path, 'w') as f:
                if merge_logs:
                    f.write(stdout_log.name + "\n")
                else:
                    f.write(stdout_log.name + "\n")
                    f.write(stderr_log.name + "\n")

            # Save the PID to file
            with open(self.get_pid_file(service_name), 'w') as f:
                f.write(str(process.pid))
                
            # 记录启动时间
            start_time = time.time()
            self.start_times[service_name] = start_time
            
            # 保存启动时间到文件
            start_time_file = self.pid_dir / f"{service_name}.time"
            with open(start_time_file, 'w') as f:
                f.write(str(start_time))
                
            logger.info(f"Started service '{service_name}' with PID {process.pid}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start service '{service_name}': {str(e)}")
            return False
    
    def _backup_log_file(self, log_path: Path):
        """
        Backs up a log file by adding a numeric suffix.
        e.g., app.log -> app.log.1
        """
        if not log_path.exists():
            return

        i = 1
        while True:
            backup_path = Path(f"{str(log_path)}.{i}")
            if not backup_path.exists():
                try:
                    # Use shutil.move for cross-filesystem compatibility
                    shutil.move(str(log_path), str(backup_path))
                    logger.info(f"Backed up existing log file {log_path} to {backup_path}")
                except Exception as e:
                    logger.error(f"Failed to back up log file {log_path}: {e}")
                break
            i += 1

    # Default stop tuning (overridable per-service in pmo.yml or via the CLI).
    DEFAULT_STOP_TIMEOUT = 5      # seconds to wait for graceful SIGTERM exit
    DEFAULT_STOP_KILL_ROUNDS = 3  # max SIGKILL (-9) waves before giving up
    DEFAULT_STOP_KILL_WAIT = 3    # seconds to wait after each SIGKILL wave

    def stop(self, service_name: str, timeout: Optional[int] = None,
             kill_rounds: Optional[int] = None, kill_wait: Optional[int] = None) -> bool:
        """Stop a service by recursively killing its whole process tree.

        Gather the root process and all of its descendants, send SIGTERM to the
        process group (graceful), wait up to `timeout` seconds, then send up to
        `kill_rounds` waves of SIGKILL (-9) — waiting `kill_wait` seconds after
        each — until nothing is left. Zombie/defunct processes are ignored (they
        are already dead and get reaped automatically). Never waits for input.

        Each tuning value falls back to the per-service `stop_timeout` /
        `stop_kill_rounds` / `stop_kill_wait` key in pmo.yml, then to the
        class defaults. Explicit arguments (e.g. from the CLI) win.
        """
        conf = self.services.get(service_name, {}) or {}
        if timeout is None:
            timeout = int(conf.get("stop_timeout", self.DEFAULT_STOP_TIMEOUT))
        if kill_rounds is None:
            kill_rounds = int(conf.get("stop_kill_rounds", self.DEFAULT_STOP_KILL_ROUNDS))
        if kill_wait is None:
            kill_wait = int(conf.get("stop_kill_wait", self.DEFAULT_STOP_KILL_WAIT))

        pid = self.get_service_pid(service_name)
        if not pid:
            logger.info(f"Service '{service_name}' is not running.")
            return True

        try:
            # 1) Snapshot the full process tree: root + all recursive
            #    descendants, unioned with every member of its process group
            #    (covers multi-process tasks the recursive walk might miss).
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, PermissionError):
                pgid = None
            pids = self.get_process_tree(pid)
            for gp in self._group_pids(pgid):
                if gp not in pids:
                    pids.append(gp)
            logger.info(f"Stopping service '{service_name}' ({len(pids)} processes)...")

            # 2) Graceful shutdown: SIGTERM the group + every known PID.
            console.print(f"[yellow]📤[/] Sending SIGTERM to {len(pids)} processes...")
            self._signal_tree(pgid, pids, signal.SIGTERM)

            # 3) Wait up to `timeout`s for them to exit on their own.
            for elapsed in range(1, timeout + 1):
                alive = self._active_pids(pids)
                if not alive:
                    console.print(f"[green]✓[/] All processes terminated gracefully")
                    break
                console.print(f"[dim]Waiting for {len(alive)} process(es) to exit... {elapsed}/{timeout}s[/]")
                if elapsed < timeout:
                    time.sleep(1)

            # 4) Force kill survivors with up to `kill_rounds` SIGKILL waves.
            #    Re-scan the tree + group first to catch children spawned
            #    during the grace period.
            alive = self._surviving_pids(pids, pgid)
            if alive:
                for p in list(alive):
                    for child in self.get_process_tree(p):
                        if child not in pids:
                            pids.append(child)
                for gp in self._group_pids(pgid):
                    if gp not in pids:
                        pids.append(gp)

                for round_num in range(1, kill_rounds + 1):
                    alive = self._surviving_pids(pids, pgid)
                    if not alive:
                        break
                    console.print(f"[red]💀[/] SIGKILL (-9) round {round_num}/{kill_rounds}: killing {len(alive)} process(es)...")
                    self._signal_tree(pgid, pids, signal.SIGKILL)
                    # Hold and poll for `kill_wait`s; stop early once all gone.
                    for _ in range(max(kill_wait, 1) * 2):
                        if not self._active_pids(pids):
                            break
                        time.sleep(0.5)

            # 5) Clean up bookkeeping files (PMO keeps no daemon).
            self._cleanup_service_files(service_name)

            # 6) Report. Zombies do not count as failures.
            alive = self._surviving_pids(pids, pgid)
            if alive:
                console.print(f"[red]✗[/] Service '{service_name}': {len(alive)} process(es) still alive after {kill_rounds}x SIGKILL -9 (likely stuck in uninterruptible I/O):")
                for line in self._ps_lines(alive):
                    console.print(line, style="red", markup=False)
                console.print(f"[dim]Retry the stop, or run manually: kill -9 {' '.join(map(str, alive))}[/]")
                logger.error(f"Service '{service_name}' stop failed: {len(alive)} process(es) still alive: {alive}")
                return False

            logger.info(f"Service '{service_name}' stopped successfully")
            return True

        except ProcessLookupError:
            # Process already gone.
            self._cleanup_service_files(service_name)
            logger.info(f"Service '{service_name}' was not running")
            return True
        except Exception as e:
            logger.error(f"Failed to stop service '{service_name}': {str(e)}")
            return False
    
    def _cleanup_service_files(self, service_name: str):
        """Clean up service-related files."""
        pid_file = self.get_pid_file(service_name)
        if os.path.exists(pid_file):
            os.remove(pid_file)

        # 删除 .logfile 文件
        logfile_hint_path = self.log_dir / f"{service_name}.logfile"
        if logfile_hint_path.exists():
            os.remove(logfile_hint_path)

        if service_name in self.start_times:
            del self.start_times[service_name]

        start_time_file = self.pid_dir / f"{service_name}.time"
        if start_time_file.exists():
            os.remove(start_time_file)
    
    def restart(self, service_name: str) -> bool:
        """Restart a service."""
        self.stop(service_name)
        result = self.start(service_name)
        if result:
            # 增加重启次数
            self.restarts[service_name] = self.restarts.get(service_name, 0) + 1
            # 保存重启次数到文件
            restart_file = self.pid_dir / f"{service_name}.restarts"
            with open(restart_file, 'w') as f:
                f.write(str(self.restarts[service_name]))
        return result
    
    def get_process_stats(self, service_name: str) -> Dict[str, Any]:
        """获取进程的 CPU 和内存使用情况"""
        pid = self.get_service_pid(service_name)
        stats = {"cpu_percent": None, "memory_percent": None, "memory_mb": None}
        
        if pid:
            try:
                process = psutil.Process(pid)
                # 获取 CPU 使用百分比 (非阻塞模式)
                stats["cpu_percent"] = process.cpu_percent(interval=0)
                
                # 获取内存使用情况
                memory_info = process.memory_info()
                stats["memory_mb"] = memory_info.rss / (1024 * 1024)  # 转换为 MB
                
                # 计算内存使用百分比
                stats["memory_percent"] = process.memory_percent()
                
                # 获取GPU信息 - 从进程树中获取所有进程
                gpu_stats = self.get_gpu_stats_for_process_tree(pid)
                stats.update(gpu_stats)
                
                return stats
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # 如果进程已不存在或无法访问，返回默认值
                pass
                
        return stats
    
    def get_process_tree(self, pid: int) -> List[int]:
        """获取进程及其所有子进程的PID列表"""
        try:
            process = psutil.Process(pid)
            children = process.children(recursive=True)
            return [pid] + [child.pid for child in children]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return [pid]
    
    def get_process_tree_info(self, service_name: str) -> Dict[str, Any]:
        """获取服务进程树的详细信息"""
        pid = self.get_service_pid(service_name)
        if not pid:
            return {
                "main_process": None,
                "children": [],
                "total_processes": 0,
                "total_cpu": 0.0,
                "total_memory": 0.0
            }
        
        try:
            main_process = psutil.Process(pid)
            children = main_process.children(recursive=True)
            
            # 主进程信息
            main_info = {
                "pid": pid,
                "name": main_process.name(),
                "cmdline": " ".join(main_process.cmdline()),
                "cpu_percent": main_process.cpu_percent(interval=0),
                "memory_mb": main_process.memory_info().rss / (1024 * 1024),
                "memory_percent": main_process.memory_percent(),
                "status": main_process.status(),
                "create_time": main_process.create_time()
            }
            
            # 子进程信息
            children_info = []
            total_cpu = main_info["cpu_percent"]
            total_memory = main_info["memory_mb"]
            
            for child in children:
                try:
                    child_info = {
                        "pid": child.pid,
                        "name": child.name(),
                        "cmdline": " ".join(child.cmdline()),
                        "cpu_percent": child.cpu_percent(interval=0),
                        "memory_mb": child.memory_info().rss / (1024 * 1024),
                        "memory_percent": child.memory_percent(),
                        "status": child.status(),
                        "create_time": child.create_time()
                    }
                    children_info.append(child_info)
                    total_cpu += child_info["cpu_percent"]
                    total_memory += child_info["memory_mb"]
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    # 子进程可能已经退出，跳过
                    continue
            
            return {
                "main_process": main_info,
                "children": children_info,
                "total_processes": 1 + len(children_info),
                "total_cpu": total_cpu,
                "total_memory": total_memory
            }
            
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return {
                "main_process": None,
                "children": [],
                "total_processes": 0,
                "total_cpu": 0.0,
                "total_memory": 0.0
            }
    
    def get_gpu_stats_for_process_tree(self, pid: int) -> Dict[str, Any]:
        """获取进程树中所有进程的GPU使用情况"""
        result = {
            "gpu_memory": None,
            "gpu_bus_id": None,
            "gpu_id": None
        }
        
        # 首先检查是否有pynvml库
        try:
            import pynvml
            return self._get_gpu_stats_pynvml(pid)
        except ImportError:
            # 如果没有pynvml，回退到nvidia-smi命令
            pass
        
        try:
            # 获取进程树中的所有PID
            process_tree_pids = self.get_process_tree(pid)
            
            # 检查nvidia-smi命令是否存在
            if not self._is_command_available("nvidia-smi"):
                logger.warning("nvidia-smi command not available")
                return result
            
            # 先获取所有GPU设备信息，用于映射总线ID到设备ID
            cmd_devices = ["nvidia-smi", "--query-gpu=index,gpu_bus_id", "--format=csv,noheader"]
            output_devices = subprocess.check_output(cmd_devices, universal_newlines=True)
            
            # 解析设备信息，创建总线ID到设备ID的映射
            pci_to_device_id = {}
            for line in output_devices.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(', ')
                if len(parts) == 2:
                    device_idx = parts[0].strip()
                    bus_id = parts[1].strip()
                    pci_to_device_id[bus_id] = device_idx
            
            # 使用nvidia-smi获取GPU信息
            cmd = ["nvidia-smi", "--query-compute-apps=pid,gpu_name,used_memory,gpu_bus_id", "--format=csv,noheader"]
            output = subprocess.check_output(cmd, universal_newlines=True)
            
            # 解析输出
            for line in output.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(', ')
                if len(parts) >= 4:
                    try:
                        process_pid = int(parts[0].strip())
                        if process_pid in process_tree_pids:
                            result["gpu_memory"] = parts[2].strip()
                            bus_id = parts[3].strip()
                            result["gpu_bus_id"] = bus_id
                            
                            # 将总线ID转换为设备ID
                            result["gpu_id"] = pci_to_device_id.get(bus_id, "?")
                            break
                    except (ValueError, IndexError):
                        continue
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            logger.debug(f"Error getting GPU stats: {str(e)}")
        
        return result
    
    def _get_gpu_stats_pynvml(self, pid: int) -> Dict[str, Any]:
        """使用pynvml获取GPU信息"""
        import pynvml
        result = {
            "gpu_memory": None,
            "gpu_bus_id": None,
            "gpu_id": None
        }
        
        try:
            # 初始化NVML库
            pynvml.nvmlInit()
            
            # 获取进程树
            process_tree_pids = self.get_process_tree(pid)
            
            # 获取设备数量
            device_count = pynvml.nvmlDeviceGetCount()
            
            # 遍历每个GPU设备
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                
                # 获取进程信息
                processes = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                
                for process in processes:
                    if process.pid in process_tree_pids:
                        # 将内存从字节转换为MB
                        memory_mb = process.usedGpuMemory / (1024 * 1024)
                        result["gpu_memory"] = f"{int(memory_mb)} MiB"
                        
                        # 获取GPU总线ID
                        bus_id = pynvml.nvmlDeviceGetPciInfo(handle).busId
                        if isinstance(bus_id, bytes):
                            bus_id = bus_id.decode('utf-8')
                        result["gpu_bus_id"] = bus_id
                        
                        # 直接使用设备索引作为GPU ID
                        result["gpu_id"] = str(i)
                        break
            
            # 关闭NVML
            pynvml.nvmlShutdown()
            
        except Exception as e:
            logger.debug(f"Error getting GPU stats using pynvml: {str(e)}")
            
        return result
    
    def _is_command_available(self, cmd: str) -> bool:
        """检查命令是否可用"""
        return shutil.which(cmd) is not None
    
    def format_cpu_percent(self, cpu_percent: Optional[float]) -> str:
        """格式化 CPU 使用百分比"""
        if cpu_percent is None:
            return "0%"
        return f"{cpu_percent:.1f}%"
    
    def format_memory(self, memory_mb: Optional[float], memory_percent: Optional[float]) -> str:
        """格式化内存使用情况"""
        if memory_mb is None:
            return "0b"
        
        # 如果小于 1MB，显示为 KB
        if memory_mb < 1:
            return f"{int(memory_mb * 1024)}kb"
        
        # 如果大于 1GB，显示为 GB
        if memory_mb > 1024:
            return f"{memory_mb/1024:.1f}gb"
            
        # 否则显示为 MB
        return f"{int(memory_mb)}mb"

    def format_gpu_memory(self, gpu_memory: Optional[str]) -> str:
        """格式化GPU内存使用"""
        if gpu_memory is None:
            return "0"
        return gpu_memory

    def get_restarts_count(self, service_name: str) -> int:
        """获取服务重启次数"""
        return self.restarts.get(service_name, 0)
    
    def get_all_hostnames(self) -> List[str]:
        """获取所有存在服务的主机名"""
        hostnames = []
        try:
            if self.pmo_base_dir.exists():
                for hostname_dir in self.pmo_base_dir.iterdir():
                    if hostname_dir.is_dir():
                        # 检查是否有 pids 或 logs 目录
                        pids_dir = hostname_dir / "pids"
                        logs_dir = hostname_dir / "logs"
                        if pids_dir.exists() or logs_dir.exists():
                            # 检查是否有任何文件
                            has_files = False
                            if pids_dir.exists():
                                has_files = any(pids_dir.iterdir())
                            if not has_files and logs_dir.exists():
                                has_files = any(logs_dir.iterdir())
                            if has_files:
                                hostnames.append(hostname_dir.name)
        except Exception as e:
            logger.debug(f"Error getting hostnames: {e}")
        return sorted(hostnames)
    
    def get_remote_log_dir(self, hostname: str) -> Path:
        """获取指定主机名的日志目录"""
        return self.pmo_base_dir / hostname / "logs"
    
    def get_remote_pid_dir(self, hostname: str) -> Path:
        """获取指定主机名的PID目录"""
        return self.pmo_base_dir / hostname / "pids"
    
    def get_remote_service_names(self, hostname: str) -> List[str]:
        """获取指定主机名的服务名称列表"""
        service_names = []
        try:
            pid_dir = self.get_remote_pid_dir(hostname)
            if pid_dir.exists():
                for pid_file in pid_dir.glob("*.pid"):
                    service_name = pid_file.stem
                    service_names.append(service_name)
        except Exception as e:
            logger.debug(f"Error getting remote service names for {hostname}: {e}")
        return sorted(service_names)
    
    def get_remote_service_id_map(self, hostname: str) -> Dict[str, str]:
        """获取指定主机名的服务ID映射"""
        service_names = self.get_remote_service_names(hostname)
        return {name: str(i + 1) for i, name in enumerate(service_names)}
