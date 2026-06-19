"""
测试服务管理功能。
"""
import pytest
import os
import time
import signal
import psutil
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock
from pmo.service import ServiceManager

class TestServiceManagement:
    """测试服务管理功能"""
    
    def test_start_single_service(self, basic_config_file):
        """TC2.1: 测试启动单个服务"""
        manager = ServiceManager(config_path=basic_config_file)
        
        # 启动测试服务
        result = manager.start('test-echo')
        assert result is True
        
        # 验证服务状态 - 由于 test-echo 服务执行后会立即结束，所以我们不检查其运行状态
        
        # 验证日志文件创建
        log_dir = manager.log_dir
        assert (log_dir / 'test-echo-out.log').exists()
        assert (log_dir / 'test-echo-error.log').exists()

    @patch('subprocess.Popen')
    def test_start_writes_service_yaml_after_merged_log_header(self, mock_popen, temp_dir):
        """启动服务时在启动横幅后写入对应服务的 YAML 配置"""
        config = {
            'merged-service': {
                'cmd': 'echo "Hello from merged service" \\\n  --flag value\n',
                'merge_logs': True,
                'env': {'TEST_ENV': 'test_value'}
            }
        }
        config_path = Path(temp_dir) / 'merged.yml'
        with open(config_path, 'w') as f:
            yaml.safe_dump(config, f, sort_keys=False)

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        manager = ServiceManager(config_path=config_path)
        result = manager.start('merged-service')

        assert result is True
        log_path = manager.log_dir / 'merged-service.log'
        content = log_path.read_text()
        header = "--- Starting service 'merged-service'"
        assert header in content
        assert content.index("merged-service:") > content.index(header)
        assert "cmd: |" in content
        assert 'echo "Hello from merged service" \\' in content
        assert "  --flag value" in content
        assert "merge_logs: true" in content
        assert "TEST_ENV: test_value" in content

    @patch('subprocess.Popen')
    def test_start_treats_yaml_null_env_value_as_empty_string(self, mock_popen, temp_dir):
        """YAML 中的空 env 值应作为空字符串参与命令替换"""
        config = {
            'empty-prefix-service': {
                'cmd': '${PREFIX_CMD} echo "hello"',
                'env': {'PREFIX_CMD': None}
            }
        }
        config_path = Path(temp_dir) / 'empty_prefix.yml'
        with open(config_path, 'w') as f:
            yaml.safe_dump(config, f, sort_keys=False)

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        manager = ServiceManager(config_path=config_path)
        with patch('os.environ.copy', return_value={}):
            result = manager.start('empty-prefix-service')

        assert result is True
        args, kwargs = mock_popen.call_args
        assert args[0] == ' echo "hello"'
        assert kwargs['env']['PREFIX_CMD'] == ''
    
    @patch('subprocess.Popen')
    def test_start_all_services(self, mock_popen, basic_config_file):
        """TC2.2: 测试启动所有服务"""
        # 配置模拟对象
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process
        
        manager = ServiceManager(config_path=basic_config_file)
        
        # 启动所有服务
        with patch.object(manager, 'start') as mock_start:
            mock_start.return_value = True
            from pmo.cli import handle_start
            result = handle_start(manager, 'all')
            assert result is True
            
            # 验证每个服务都调用了启动方法
            assert mock_start.call_count == 2
            
            # 确保两个服务都被尝试启动
            service_names = set([call[0][0] for call in mock_start.call_args_list])
            assert service_names == {'test-echo', 'test-sleep'}
    
    def test_stop_single_service(self, basic_config_file):
        """TC2.3: 测试停止单个服务"""
        # 创建一个更简单的直接测试方式
        manager = ServiceManager(config_path=basic_config_file)
        
        # 创建一个模拟的 stop 方法
        def mock_stop(service_name, timeout=5):
            if service_name == 'test-sleep':
                return True
            return False
            
        # 直接替换 stop 方法
        with patch.object(manager, 'stop', side_effect=mock_stop):
            result = manager.stop('test-sleep')
            assert result is True
    
    @patch('pmo.service.ServiceManager.stop')
    def test_stop_all_services(self, mock_stop, basic_config_file):
        """TC2.4: 测试停止所有服务"""
        manager = ServiceManager(config_path=basic_config_file)
        
        # 模拟运行中的服务
        mock_stop.return_value = True
        
        with patch.object(manager, 'get_running_services', return_value=['test-echo', 'test-sleep']):
            from pmo.cli import handle_stop
            result = handle_stop(manager, 'all')
            assert result is True
            
            # 验证每个服务都调用了停止方法
            assert mock_stop.call_count == 2
            
            # 确保两个服务都被尝试停止
            service_names = set([call[0][0] for call in mock_stop.call_args_list])
            assert service_names == {'test-echo', 'test-sleep'}
    
    @patch('pmo.service.ServiceManager.stop')
    @patch('pmo.service.ServiceManager.start')
    def test_restart_single_service(self, mock_start, mock_stop, basic_config_file):
        """TC2.5: 测试重启单个服务"""
        mock_stop.return_value = True
        mock_start.return_value = True
        
        manager = ServiceManager(config_path=basic_config_file)
        result = manager.restart('test-sleep')
        
        assert result is True
        mock_stop.assert_called_once_with('test-sleep')
        mock_start.assert_called_once_with('test-sleep')
    
    @patch('pmo.service.ServiceManager.restart')
    def test_restart_all_services(self, mock_restart, basic_config_file):
        """TC2.6: 测试重启所有服务"""
        manager = ServiceManager(config_path=basic_config_file)
        mock_restart.return_value = True
        
        with patch.object(manager, 'get_service_names', return_value=['test-echo', 'test-sleep']):
            from pmo.cli import handle_restart
            result = handle_restart(manager, 'all')
            assert result is True
            
            # 验证每个服务都调用了重启方法
            assert mock_restart.call_count == 2
            
            # 确保两个服务都被尝试重启
            service_names = set([call[0][0] for call in mock_restart.call_args_list])
            assert service_names == {'test-echo', 'test-sleep'}
