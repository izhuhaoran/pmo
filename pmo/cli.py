#!/usr/bin/env python3
"""
Command-line interface for PMO process manager.
Using Rich library for formatted terminal output
"""
import os
import sys
import socket
import argparse
import logging
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from pmo.service import ServiceManager
from pmo.logs import LogManager, Emojis
from pmo.logs import console, print_header, print_info, print_warning, print_error, print_success, print_service_table

from rich.text import Text
from rich.traceback import install, Traceback
from rich.markup import escape

# Install Rich exception formatter
install()

# Per-level styles, resolved against the theme in pmo.logs.console
_LOG_LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "error",
}


class ConsoleLogHandler(logging.Handler):
    """Plain single-line log handler (vLLM-style).

    Renders each record as one inline line:
        LEVEL MM-DD HH:MM:SS.mmm [file.py:line] message
      * no table layout; level / time / location sit inline at the front
      * continuation starts at column 0 instead of aligning under the message
      * soft wrap only, so Rich never hard-wraps to the terminal width
      * file paths keep clean whitespace boundaries so they stay clickable
    """

    def emit(self, record):
        try:
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(record.created))
            ts = f"{ts}.{int(record.msecs):03d}"
            style = _LOG_LEVEL_STYLES.get(record.levelname, "info")
            line = Text.assemble(
                (f"{record.levelname} ", style),
                (f"{ts} ", "dim"),
                (f"[{record.filename}:{record.lineno}] ", "dim"),
                record.getMessage(),
            )
            console.print(line, soft_wrap=True)
            if record.exc_info:
                console.print(Traceback.from_exception(*record.exc_info))
        except Exception:
            self.handleError(record)


logging.basicConfig(level=logging.INFO, handlers=[ConsoleLogHandler()])

logger = logging.getLogger("pmo")

def setup_arg_parser() -> argparse.ArgumentParser:
    """Set up command line argument parser"""
    parser = argparse.ArgumentParser(
        description=f"{Emojis.SERVICE} PMO - Modern process manager",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Global parameters
    parser.add_argument('-f', '--config', 
                      help='Config file path (default: pmo.yml)',
                      default='pmo.yml')
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Start command
    start_parser = subparsers.add_parser('start', help=f'{Emojis.START} Start services')
    start_parser.add_argument('service', nargs='*', 
                            help='Service names or IDs (multiple allowed) or "all" to start all services')
    start_parser.add_argument('--dry-run', action='store_true',
                            help='Show commands to execute without running them')
    
    # Dry-run command (shortcut for start --dry-run)
    dry_run_parser = subparsers.add_parser('dry-run', help=f'{Emojis.INFO} Show commands to execute without running them')
    dry_run_parser.add_argument('service', nargs='*', 
                              help='Service names or IDs (multiple allowed) or "all" to show all service commands')
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help=f'{Emojis.STOP} Stop services')
    stop_parser.add_argument('service', nargs='*',
                           help='Service names or IDs (multiple allowed) or "all" to stop all services')
    stop_parser.add_argument('--timeout', '-t', type=int, default=None,
                           help='Seconds to wait for graceful SIGTERM shutdown before SIGKILL (default: 5)')
    stop_parser.add_argument('--kill-rounds', type=int, default=None,
                           help='Max number of SIGKILL (-9) rounds for stuck processes (default: 3)')
    stop_parser.add_argument('--kill-wait', type=int, default=None,
                           help='Seconds to wait after each SIGKILL (-9) round (default: 3)')
    
    # Restart command
    restart_parser = subparsers.add_parser('restart', help=f'{Emojis.RESTART} Restart services')
    restart_parser.add_argument('service', nargs='*',
                              help='Service names or IDs (multiple allowed) or "all" to restart all services')
    
    # Log command with 'logs' as an alias
    log_parser = subparsers.add_parser('logs', aliases=['log'], help=f'{Emojis.LOG} View service logs')
    log_parser.add_argument('service', nargs='*', default=['all'],
                          help='Service names or IDs (multiple allowed) or "all" to view all logs')
    log_parser.add_argument('--no-follow', '-n', action='store_true',
                          help='Do not follow logs in real-time')
    log_parser.add_argument('--lines', '-l', type=int, 
                          help='Number of lines to show initially (default: 15 for all services, 30 for specific services)')
    
    # Flush command
    flush_parser = subparsers.add_parser('flush', help=f'{Emojis.LOG} Clear service logs')
    flush_parser.add_argument('service', nargs='*', default=['all'],
                          help='Service names or IDs (multiple allowed) or "all" to clear all logs')
    
    # LS command (replaces original PS command)
    ls_parser = subparsers.add_parser('ls', help='List services')
    
    # Status command
    status_parser = subparsers.add_parser('status', aliases=['st'], help=f'{Emojis.INFO} Show detailed service status with process tree')
    status_parser.add_argument('service', nargs='*', default=['all'],
                             help='Service names or IDs (multiple allowed) or "all" to show all services')

    # Pipeline command (sequential task execution)
    pipeline_parser = subparsers.add_parser('pipeline', help=f'{Emojis.START} Run tasks sequentially (start, wait, stop, next)')
    pipeline_parser.add_argument('service', nargs='+',
                                help='Service names or IDs to run in order')
    pipeline_parser.add_argument('--sleep', type=int, default=5,
                                help='Seconds to sleep between tasks (default: 5)')
    pipeline_parser.add_argument('--flush', action='store_true',
                                help='Flush logs before each task')
    pipeline_parser.add_argument('--poll-interval', type=int, default=10,
                                help='Seconds between status polls (default: 10)')

    return parser

def show_service_prompt(manager: ServiceManager, command: str) -> None:
    """Helper function to show prompt when no service is specified"""
    print_warning(f"Please specify a service name or 'all' to {command} all services.")
    print_info(f"Usage: pmo {command} <service-name> or pmo {command} all")
    service_names = manager.get_service_names()
    if service_names:
        print_info(f"Available services: {', '.join(service_names)}")

def handle_start(manager: ServiceManager, service_specs: List[str], dry_run: bool = False) -> bool:
    """Handle start command with support for multiple services"""
    # Check if no services specified
    if not service_specs:
        show_service_prompt(manager, "start")
        return False
    
    # Resolve service names, handling possible IDs or 'all'
    service_names = resolve_multiple_services(manager, service_specs)
    
    if not service_names:
        print_warning("No valid services specified for starting.")
        return False
    
    # If coming from 'all', provide appropriate message
    if 'all' in service_specs:
        if dry_run:
            console.print(f"{Emojis.INFO} Commands to execute (dry run):", style="running")
        else:
            console.print(f"{Emojis.START} Starting all services...", style="running")
    elif len(service_names) > 1:
        if dry_run:
            console.print(f"{Emojis.INFO} Commands to execute for selected services (dry run):", style="running")
        else:
            console.print(f"{Emojis.START} Starting selected services...", style="running")
    
    # Start each service
    success = True
    for name in service_names:
        if not manager.start(name, dry_run=dry_run):
            print_error(f"Failed to start '{name}'")
            success = False
        else:
            if not dry_run:
                print_success(f"Service '{name}' started")
    
    # Final status message
    if len(service_names) > 1:
        if success and not dry_run:
            print_success("All specified services started successfully!")
        elif not success and not dry_run:
            print_warning("Some services failed to start, check logs for details.")
    
    return success

def handle_stop(manager: ServiceManager, service_specs: List[str],
                timeout: Optional[int] = None, kill_rounds: Optional[int] = None,
                kill_wait: Optional[int] = None) -> bool:
    """Handle stop command with support for multiple services"""
    # Check if no services specified
    if not service_specs:
        show_service_prompt(manager, "stop")
        return False
    
    # Resolve service names, handling possible IDs or 'all'
    if 'all' in service_specs:
        service_names = manager.get_running_services()
        if not service_names:
            console.print(f"{Emojis.INFO} No services are running.", style="dim")
            return True
        console.print(f"{Emojis.STOP} Stopping all services...", style="warning")
    else:
        # Resolve specific services
        service_names = []
        for spec in service_specs:
            service_name = resolve_service_id(manager, spec)
            if service_name and service_name not in service_names:
                # Check if service is running before stopping
                if manager.is_running(service_name):
                    service_names.append(service_name)
                else:
                    console.print(f"{Emojis.INFO} Service '{service_name}' is not running.", style="dim")
        
        if not service_names:
            print_warning("No running services specified for stopping.")
            return True
            
        if len(service_names) > 1:
            console.print(f"{Emojis.STOP} Stopping selected services...", style="warning")
    
    # Stop each service
    success = True
    for name in service_names:
        if not manager.stop(name, timeout=timeout, kill_rounds=kill_rounds, kill_wait=kill_wait):
            print_error(f"Failed to stop '{name}'")
            success = False
        else:
            console.print(f"{Emojis.STOPPED} Service '{name}' stopped", style="stopped")
    
    # Final status message
    if len(service_names) > 1:
        if success:
            console.print(f"\n{Emojis.STOPPED} All specified services stopped successfully!", style="warning")
        else:
            print_warning("Some services failed to stop, check logs for details.")
    
    return success

def handle_restart(manager: ServiceManager, service_specs: List[str]) -> bool:
    """Handle restart command with support for multiple services"""
    # Check if no services specified
    if not service_specs:
        show_service_prompt(manager, "restart")
        return False
    
    # Resolve service names, handling possible IDs or 'all'
    service_names = resolve_multiple_services(manager, service_specs)
    
    if not service_names:
        print_warning("No valid services specified for restarting.")
        return False
    
    # If coming from 'all', provide appropriate message
    if 'all' in service_specs:
        console.print(f"{Emojis.RESTART} Restarting all services...", style="restart")
    elif len(service_names) > 1:
        console.print(f"{Emojis.RESTART} Restarting selected services...", style="restart")
    
    # Restart each service
    success = True
    for name in service_names:
        if not manager.restart(name):
            print_error(f"Failed to restart '{name}'")
            success = False
        else:
            console.print(f"{Emojis.RUNNING} Service '{name}' restarted", style="restart")
    
    # Final status message
    if len(service_names) > 1:
        if success:
            print_success("All specified services restarted successfully!")
        else:
            print_warning("Some services failed to restart, check logs for details.")
    
    return success

def handle_log(manager: ServiceManager, log_manager: LogManager, args) -> bool:
    """Handle log command with support for multiple services and remote hostnames"""
    service_specs = args.service
    follow = not args.no_follow
    
    # Set default line values based on whether specific services are specified
    if args.lines is None:
        if service_specs == ['all']:
            lines = 15  # Default for 'all' services
        else:
            lines = 30  # Default when specific services are specified
    else:
        lines = args.lines
    
    # Check if no services specified (should not happen due to default=['all'])
    if not service_specs:
        service_specs = ['all']
    
    # Group services by hostname
    local_services = []
    remote_services = {}  # hostname -> [service_names]
    
    for service_spec in service_specs:
        hostname, services = resolve_remote_service_spec(manager, service_spec)
        
        if hostname is None:
            # Local services
            local_services.extend(services)
        else:
            # Remote services
            if hostname not in remote_services:
                remote_services[hostname] = []
            remote_services[hostname].extend(services)
    
    # Handle local services
    if local_services:
        all_services = manager.get_service_names()
        service_id_map = {name: str(idx + 1) for idx, name in enumerate(all_services)}
        # 传递服务配置，以便支持合并日志
        log_manager.tail_logs(local_services, follow=follow, lines=lines, service_id_map=service_id_map, service_configs=manager.services)
    
    # Handle remote services
    for hostname, services in remote_services.items():
        if services:
            console.print(f"\n[cyan]--- Logs from {hostname} ---[/]")
            remote_log_dir = manager.get_remote_log_dir(hostname)
            remote_log_manager = LogManager(remote_log_dir)
            service_id_map = manager.get_remote_service_id_map(hostname)
            # 远程服务暂时不传递配置，因为无法获取远程配置
            remote_log_manager.tail_logs(services, follow=follow, lines=lines, service_id_map=service_id_map, hostname=hostname)
    
    return True

def handle_flush(manager: ServiceManager, log_manager: LogManager, service_specs: List[str]) -> bool:
    """Handle flush command to clear logs with support for multiple services and remote hostnames"""
    # Check if no services specified (should not happen due to default=['all'])
    if not service_specs:
        service_specs = ['all']

    # Get running services list for proper log handling
    running_services = manager.get_running_services()
    
    # Group services by hostname
    local_services = []
    remote_services = {}  # hostname -> [service_names]
    
    for service_spec in service_specs:
        hostname, services = resolve_remote_service_spec(manager, service_spec)
        
        if hostname is None:
            # Local services
            local_services.extend(services)
        else:
            # Remote services
            if hostname not in remote_services:
                remote_services[hostname] = []
            remote_services[hostname].extend(services)
    
    # Handle local services
    if local_services:
        if 'all' in service_specs:
            console.print(f"{Emojis.LOG} Flushing all logs...", style="warning")
            result = log_manager.flush_logs(running_services=running_services)
            deleted_count = result.get("deleted", 0)
            cleared_count = result.get("cleared", 0)
            
            if deleted_count > 0 or cleared_count > 0:
                if deleted_count > 0:
                    print_success(f"Successfully deleted {deleted_count} log files for non-running services")
                if cleared_count > 0:
                    print_success(f"Successfully cleared content of {cleared_count} log files for running services")
            else:
                print_warning("No log files found to flush")
        else:
            if len(local_services) > 1:
                console.print(f"{Emojis.LOG} Flushing logs for selected services...", style="warning")
            
            result = log_manager.flush_logs(local_services, running_services=running_services)
            
            success_count = 0
            for service_name in local_services:
                if service_name in result:
                    service_result = result[service_name]
                    if isinstance(service_result, dict):
                        deleted_count = service_result.get("deleted", 0)
                        cleared_count = service_result.get("cleared", 0)
                        
                        if deleted_count > 0:
                            print_success(f"Successfully deleted {deleted_count} log files for '{service_name}'")
                            success_count += 1
                        if cleared_count > 0:
                            print_success(f"Successfully cleared content of {cleared_count} log files for '{service_name}'")
                            success_count += 1
                        if deleted_count == 0 and cleared_count == 0:
                            print_warning(f"No log files found for '{service_name}'")
                    else:
                        print_warning(f"No log files found for '{service_name}'")
                else:
                    print_warning(f"No log files found for '{service_name}'")
    
    # Handle remote services
    for hostname, services in remote_services.items():
        if services:
            console.print(f"\n[cyan]--- Flushing logs on {hostname} ---[/]")
            remote_log_dir = manager.get_remote_log_dir(hostname)
            remote_log_manager = LogManager(remote_log_dir)
            
            # Note: We can't know which services are running on remote hosts
            # So we'll just flush the logs (delete them if they exist)
            result = remote_log_manager.flush_logs(services, running_services=[])
            
            success_count = 0
            for service_name in services:
                if service_name in result:
                    service_result = result[service_name]
                    if isinstance(service_result, dict):
                        deleted_count = service_result.get("deleted", 0)
                        cleared_count = service_result.get("cleared", 0)
                        
                        if deleted_count > 0:
                            print_success(f"Successfully deleted {deleted_count} log files for '{hostname}:{service_name}'")
                            success_count += 1
                        if cleared_count > 0:
                            print_success(f"Successfully cleared content of {cleared_count} log files for '{hostname}:{service_name}'")
                            success_count += 1
                        if deleted_count == 0 and cleared_count == 0:
                            print_warning(f"No log files found for '{hostname}:{service_name}'")
                    else:
                        print_warning(f"No log files found for '{hostname}:{service_name}'")
                else:
                    print_warning(f"No log files found for '{hostname}:{service_name}'")
    
    return True

def handle_status(manager: ServiceManager, service_specs: List[str]) -> bool:
    """Handle status command to show detailed service status with process tree"""
    # Check if no services specified (should not happen due to default=['all'])
    if not service_specs:
        service_specs = ['all']
    
    # Resolve service names, handling possible IDs or 'all'
    service_names = resolve_multiple_services(manager, service_specs)
    
    if not service_names:
        print_warning("No valid services specified for status.")
        return False
    
    # Display status for each service
    for service_name in service_names:
        print_service_status(manager, service_name)
    
    return True

def print_service_status(manager: ServiceManager, service_name: str) -> None:
    """Print detailed status for a single service"""
    from rich.panel import Panel
    from rich.table import Table
    from rich.columns import Columns
    from datetime import datetime
    
    # Get service configuration
    config = manager.services.get(service_name, {})
    
    # Get process tree info
    tree_info = manager.get_process_tree_info(service_name)
    
    # Create main info table
    info_table = Table(show_header=False, box=None, padding=(0, 1))
    info_table.add_column("key", style="cyan")
    info_table.add_column("value", style="white")
    
    # Basic service info
    info_table.add_row("Service Name", service_name)
    info_table.add_row("Status", "🟢 Running" if manager.is_running(service_name) else "🔴 Stopped")
    info_table.add_row("Config Path", manager.config_path)
    info_table.add_row("Command", config.get("cmd", "N/A"))
    info_table.add_row("Working Dir", config.get("cwd", "N/A"))
    
    # Process info
    if tree_info["main_process"]:
        main_proc = tree_info["main_process"]
        info_table.add_row("Main PID", str(main_proc["pid"]))
        info_table.add_row("Process Name", main_proc["name"])
        info_table.add_row("Total Processes", str(tree_info["total_processes"]))
        
        # Uptime
        uptime_seconds = manager.get_uptime(service_name)
        uptime_str = manager.format_uptime(uptime_seconds) if uptime_seconds else "N/A"
        info_table.add_row("Uptime", uptime_str)
        
        # Resource usage
        info_table.add_row("Total CPU", f"{tree_info['total_cpu']:.1f}%")
        info_table.add_row("Total Memory", manager.format_memory(tree_info["total_memory"], None))
        
        # Start time
        start_time = datetime.fromtimestamp(main_proc["create_time"])
        info_table.add_row("Started At", start_time.strftime("%Y-%m-%d %H:%M:%S"))
        
        # Restart count
        restart_count = manager.get_restarts_count(service_name)
        info_table.add_row("Restarts", str(restart_count))
        
        # GPU info
        stats = manager.get_process_stats(service_name)
        if stats.get("gpu_memory"):
            info_table.add_row("GPU Memory", stats["gpu_memory"])
            info_table.add_row("GPU ID", stats.get("gpu_id", "N/A"))
    else:
        info_table.add_row("Main PID", "N/A")
        info_table.add_row("Process Name", "N/A")
        info_table.add_row("Total Processes", "0")
        info_table.add_row("Uptime", "N/A")
        info_table.add_row("Total CPU", "0%")
        info_table.add_row("Total Memory", "0b")
    
    # Create process tree table
    process_table = Table(show_header=True, header_style="header", box=None)
    process_table.add_column("PID", justify="right", width=8)
    process_table.add_column("Type", width=8)
    process_table.add_column("Name", width=15)
    process_table.add_column("CPU", justify="right", width=8)
    process_table.add_column("Memory", justify="right", width=10)
    process_table.add_column("Status", width=10)
    # Command列允许换行显示完整命令
    process_table.add_column("Command", no_wrap=False, overflow="fold")
    
    if tree_info["main_process"]:
        main_proc = tree_info["main_process"]
        process_table.add_row(
            str(main_proc["pid"]),
            "main",
            main_proc["name"],
            f"{main_proc['cpu_percent']:.1f}%",
            manager.format_memory(main_proc["memory_mb"], None),
            main_proc["status"],
            main_proc["cmdline"]
        )
        
        # Add child processes
        for child in tree_info["children"]:
            process_table.add_row(
                str(child["pid"]),
                "child",
                child["name"],
                f"{child['cpu_percent']:.1f}%",
                manager.format_memory(child["memory_mb"], None),
                child["status"],
                child["cmdline"]
            )
    else:
        process_table.add_row("N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "Service not running")
    
    # Display in panels
    info_panel = Panel(info_table, title=f"Service Info: {service_name}", border_style="blue")
    process_panel = Panel(process_table, title="Process Tree", border_style="green")
    
    console.print(info_panel)
    console.print(process_panel)
    console.print()  # Add spacing between services

def handle_list(manager: ServiceManager) -> bool:
    """Handle list services command"""
    service_names = manager.get_service_names()
    
    if not service_names:
        print_warning("No services defined in config.")
        return True
    
    # Build service list data
    services = []
    for i, name in enumerate(service_names):
        is_running = manager.is_running(name)
        pid = manager.get_service_pid(name)
        
        # Get service uptime
        uptime_seconds = manager.get_uptime(name) if is_running else None
        uptime = manager.format_uptime(uptime_seconds) if is_running else "0"
        
        # Get CPU and memory usage stats
        cpu_mem_stats = {}
        gpu_stats = {"gpu_memory": "-", "gpu_bus_id": "-", "gpu_id": "-"}
        if is_running:
            stats = manager.get_process_stats(name)
            cpu_mem_stats["cpu"] = manager.format_cpu_percent(stats["cpu_percent"])
            cpu_mem_stats["memory"] = manager.format_memory(stats["memory_mb"], stats["memory_percent"])
            
            # 获取 GPU 信息
            if stats.get("gpu_memory"):
                gpu_stats["gpu_memory"] = stats["gpu_memory"]
            if stats.get("gpu_bus_id"):
                gpu_stats["gpu_bus_id"] = stats["gpu_bus_id"]
            if stats.get("gpu_id"):
                gpu_stats["gpu_id"] = stats["gpu_id"]
        else:
            cpu_mem_stats["cpu"] = "0%"
            cpu_mem_stats["memory"] = "0b"
        
        # Get restart count
        restarts_count = manager.get_restarts_count(name)
        
        services.append({
            "id": str(i + 1),  # 添加数字ID，从1开始
            "name": name,
            "status": "running" if is_running else "stopped",
            "pid": pid,
            "uptime": uptime,
            "cpu": cpu_mem_stats["cpu"],
            "memory": cpu_mem_stats["memory"],
            "gpu_memory": gpu_stats["gpu_memory"],
            "gpu_bus_id": gpu_stats["gpu_bus_id"],
            "gpu_id": gpu_stats["gpu_id"],
            "restarts": str(restarts_count)
        })
    
    # Display services as table
    print_service_table(services)
    
    console.print(f"[dim]Config: {manager.config_path}[/]")
    console.print(f"[dim]Running: {len(manager.get_running_services())}/{len(service_names)}[/]")
    
    # Show all hostnames with services
    all_hostnames = manager.get_all_hostnames()
    current_hostname = socket.gethostname()
    
    if all_hostnames:
        console.print(f"[dim]Hostnames with services: {', '.join(all_hostnames)}[/]")
        remote_hostnames = [h for h in all_hostnames if h != current_hostname]
        if remote_hostnames:
            console.print(f"[dim]Remote hostnames: {', '.join(remote_hostnames)}[/]")
    console.print()
    
    return True

def parse_hostname_service(service_spec: str) -> Tuple[Optional[str], str]:
    """
    Parse hostname:service format
    
    Args:
        service_spec: Service specification in format "hostname:service" or just "service"
        
    Returns:
        Tuple of (hostname, service_name), where hostname can be None for local services
    """
    if ':' in service_spec:
        hostname, service_name = service_spec.split(':', 1)
        return hostname, service_name
    else:
        return None, service_spec

def resolve_remote_service_spec(manager: ServiceManager, service_spec: str) -> Tuple[Optional[str], List[str]]:
    """
    Resolve service specification that may include hostname
    
    Args:
        manager: ServiceManager instance
        service_spec: Service specification in format "hostname:service" or just "service"
        
    Returns:
        Tuple of (hostname, service_names_list)
    """
    hostname, service_part = parse_hostname_service(service_spec)
    
    if hostname is None:
        # Local service
        if service_part == "all":
            return None, manager.get_service_names()
        else:
            service_name = resolve_service_id(manager, service_part)
            if service_name:
                return None, [service_name]
            else:
                return None, []
    else:
        # Remote service
        remote_services = manager.get_remote_service_names(hostname)
        if not remote_services:
            print_error(f"No services found on hostname '{hostname}'")
            return hostname, []
        
        if service_part == "":
            # hostname: format - return all services on that host
            return hostname, remote_services
        elif service_part == "all":
            # hostname:all format - return all services on that host  
            return hostname, remote_services
        else:
            # hostname:service_id or hostname:service_name format
            try:
                service_id = int(service_part)
                if 1 <= service_id <= len(remote_services):
                    return hostname, [remote_services[service_id - 1]]
                else:
                    print_error(f"Invalid service ID '{service_id}' for hostname '{hostname}' (valid range: 1-{len(remote_services)})")
                    return hostname, []
            except ValueError:
                # Not a number, treat as service name
                if service_part in remote_services:
                    return hostname, [service_part]
                else:
                    print_error(f"Service '{service_part}' not found on hostname '{hostname}'")
                    return hostname, []

def resolve_service_id(manager: ServiceManager, service_id: str) -> Optional[str]:
    """
    将服务ID或名称解析为实际的服务名称
    支持两种格式:
    1. 数字ID (如 "1", "2", "3")
    2. 服务名称
    
    如果输入是数字，则将其视为服务列表中的索引
    如果找不到匹配的服务，返回None
    """
    if service_id == "all":
        return "all"
        
    # 尝试将输入解析为数字ID
    try:
        id_num = int(service_id)
        service_names = manager.get_service_names()
        # 如果ID是有效的索引，返回对应的服务名称
        if 1 <= id_num <= len(service_names):
            return service_names[id_num - 1]  # 用户看到的ID从1开始，但索引从0开始
        else:
            print_error(f"Invalid service ID: {service_id}")
            return None
    except ValueError:
        # 不是数字，将其视为服务名称
        if service_id in manager.get_service_names():
            return service_id
        else:
            print_error(f"Service not found: '{service_id}'")
            return None

def resolve_multiple_services(manager: ServiceManager, service_specs: List[str]) -> List[str]:
    """
    Convert a list of service IDs or names to actual service names.
    If 'all' is included in the list, returns all service names.
    
    Args:
        manager: ServiceManager instance
        service_specs: List of service IDs or names to resolve
        
    Returns:
        List of resolved service names
    """
    if not service_specs:
        return []
        
    # If 'all' is specified, return all service names
    if 'all' in service_specs:
        return manager.get_service_names()
        
    resolved_services = []
    for service_spec in service_specs:
        service_name = resolve_service_id(manager, service_spec)
        if service_name and service_name not in resolved_services:
            resolved_services.append(service_name)
    
    return resolved_services

def handle_pipeline(manager: ServiceManager, log_manager: LogManager, args) -> bool:
    """Run tasks sequentially: start -> wait for exit -> stop -> next."""
    import psutil
    import signal as _signal

    service_names = resolve_multiple_services(manager, args.service)
    if not service_names:
        print_warning("No valid services specified.")
        return False

    sleep_between = args.sleep
    flush_before = args.flush
    poll_interval = args.poll_interval
    total = len(service_names)

    console.print(f"{Emojis.START} Sequential run: {total} task(s): {', '.join(service_names)}")
    console.print(f"[dim]sleep={sleep_between}s  flush={flush_before}  poll={poll_interval}s[/]")
    console.print()

    seq_start = time.time()
    interrupted = False
    current_task = [None]

    # SIGTERM handler: stop current sub-task before exiting
    def _on_sigterm(signum, frame):
        task = current_task[0]
        console.print(f"\n{Emojis.STOP} SIGTERM received, cleaning up '{task}'...")
        if task and manager.is_running(task):
            manager.stop(task)
        sys.exit(130)

    prev_sigterm = _signal.signal(_signal.SIGTERM, _on_sigterm)

    try:
        for idx, name in enumerate(service_names, 1):
            if interrupted:
                break

            current_task[0] = name
            task_start = time.time()
            console.print(f"{'='*60}")
            console.print(f"{Emojis.START} [{idx}/{total}] Starting: {name}")
            console.print(f"{'='*60}")

            # Flush logs if requested
            if flush_before:
                running = manager.get_running_services()
                log_manager.flush_logs([name], running_services=running)
                console.print(f"[dim]Flushed logs for {name}[/]")

            # Stop if already running (stale from previous run)
            if manager.is_running(name):
                console.print(f"[yellow]Service '{name}' already running, stopping first...[/]")
                manager.stop(name)
                time.sleep(2)

            # Start
            if not manager.start(name):
                print_error(f"Failed to start '{name}', aborting sequence.")
                return False

            pid = manager.get_service_pid(name)
            console.print(f"[dim]PID: {pid}, waiting for completion...[/]")

            # Wait for process to exit
            try:
                while True:
                    current_pid = manager.get_service_pid(name)
                    if current_pid is None:
                        break
                    try:
                        proc = psutil.Process(current_pid)
                        proc.wait(timeout=poll_interval)
                        break
                    except psutil.TimeoutExpired:
                        elapsed = int(time.time() - task_start)
                        console.print(f"[dim]  {name}: running {elapsed}s...[/]")
                    except psutil.NoSuchProcess:
                        break
            except KeyboardInterrupt:
                console.print(f"\n{Emojis.STOP} Interrupted during '{name}', stopping...")
                manager.stop(name)
                interrupted = True
                continue

            task_elapsed = int(time.time() - task_start)

            # Ensure cleanup
            if manager.is_running(name):
                console.print(f"[yellow]Ensuring cleanup for '{name}'...[/]")
                manager.stop(name)

            print_success(f"[{idx}/{total}] '{name}' completed in {task_elapsed}s")

            # Sleep between tasks (skip after last)
            if idx < total and sleep_between > 0 and not interrupted:
                console.print(f"[dim]Sleeping {sleep_between}s before next task...[/]")
                try:
                    time.sleep(sleep_between)
                except KeyboardInterrupt:
                    console.print(f"\n{Emojis.STOP} Interrupted during sleep, stopping sequence.")
                    interrupted = True

    finally:
        _signal.signal(_signal.SIGTERM, prev_sigterm)
        current_task[0] = None

    total_elapsed = int(time.time() - seq_start)
    console.print()
    if interrupted:
        print_warning(f"Sequence interrupted after {idx}/{total} tasks ({total_elapsed}s total)")
        return False
    else:
        print_success(f"All {total} tasks completed ({total_elapsed}s total)")
        return True

def main():
    """CLI application entry point"""
    # Header removed for more compact output
    
    parser = setup_arg_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Create service manager
    try:
        service_manager = ServiceManager(config_path=args.config)
    except Exception as e:
        print_error(f"Error loading config file: {escape(str(e))}")
        return 1
    
    # Create log manager
    log_manager = LogManager(service_manager.log_dir)
    
    # Handle commands
    try:
        if args.command == 'start':
            success = handle_start(service_manager, args.service, args.dry_run)
        elif args.command == 'dry-run':
            # When no services are specified, default to 'all'
            services = args.service if args.service else ['all']
            success = handle_start(service_manager, services, dry_run=True)
        elif args.command == 'stop':
            success = handle_stop(service_manager, args.service,
                                  timeout=args.timeout, kill_rounds=args.kill_rounds,
                                  kill_wait=args.kill_wait)
        elif args.command == 'restart':
            success = handle_restart(service_manager, args.service)
        elif args.command == 'log' or args.command == 'logs':
            success = handle_log(service_manager, log_manager, args)
        elif args.command == 'ls':
            success = handle_list(service_manager)
        elif args.command == 'status' or args.command == 'st':
            success = handle_status(service_manager, args.service)
        elif args.command == 'flush':
            success = handle_flush(service_manager, log_manager, args.service)
        elif args.command == 'pipeline':
            success = handle_pipeline(service_manager, log_manager, args)
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        console.print(f"\n{Emojis.STOP} Operation interrupted by user", style="dim")
        return 1
    except Exception as e:
        print_error(f"Error executing command: {escape(str(e))}")
        logger.exception("Command execution error")
        return 1
        
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())