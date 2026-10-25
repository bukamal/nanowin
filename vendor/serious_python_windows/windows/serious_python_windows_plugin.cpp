#include "serious_python_windows_plugin.h"

// This must be included before many other Windows headers.
#include <windows.h>

// For getPlatformVersion; remove unless needed for your plugin implementation.
#include <VersionHelpers.h>

#include <flutter/method_channel.h>
#include <flutter/plugin_registrar_windows.h>
#include <flutter/standard_method_codec.h>

#include <memory>
#include <sstream>

#include <Python.h>

#include <codecvt>
#include <locale>
#include <map>
#include <stdlib.h>
#include <stdio.h>
#include <filesystem>
#include <format>
#include <vector>
#include <string>
#include <thread>

namespace serious_python_windows
{

  using flutter::EncodableList;
  using flutter::EncodableMap;
  using flutter::EncodableValue;

  // Dart sends strings as UTF-8. The Windows CRT's narrow APIs interpret
  // them through the process ANSI code page, which corrupts any path that
  // contains Arabic (or any other non-ANSI) characters -- exactly the case
  // where serious_python's bootstrap dies with "No module named main".
  // Convert at the boundary and use the wide CRT instead.
  static std::wstring Utf8ToWide(const std::string &value)
  {
    if (value.empty())
    {
      return std::wstring();
    }
    int length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                     value.c_str(), -1, nullptr, 0);
    if (length <= 0)
    {
      return std::wstring();
    }
    std::wstring result(static_cast<size_t>(length), L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.c_str(), -1,
                        result.data(), length);
    result.pop_back(); // drop the terminating NUL
    return result;
  }

  static bool SetEnv(const std::string &key, const std::string &value)
  {
    std::wstring wide_key = Utf8ToWide(key);
    std::wstring wide_value = Utf8ToWide(value);
    if (wide_key.empty())
    {
      return false;
    }
    return _wputenv_s(wide_key.c_str(), wide_value.c_str()) == 0;
  }

  // Add an import path directly through the Python C API. This is a second
  // line of defence: even if the interpreter's own PYTHONPATH parsing used a
  // narrow conversion somewhere, sys.path still contains the real UTF-8 path.
  static void InsertSysPath(const std::string &path, int index = 0)
  {
    PyObject *sys_path = PySys_GetObject("path"); // borrowed reference
    if (sys_path == nullptr)
    {
      PyErr_Clear();
      return;
    }
    PyObject *item = PyUnicode_FromString(path.c_str()); // decodes UTF-8
    if (item == nullptr)
    {
      PyErr_Clear();
      return;
    }
    if (PyList_Insert(sys_path, index, item) != 0)
    {
      PyErr_Clear();
    }
    Py_DECREF(item);
  }

  // os.environ is what the Flet app reads for FLET_APP_STORAGE_DATA,
  // FLET_APP_CONSOLE, etc. Set those as real Python Unicode values so the
  // app's database/log paths are not mangled either.
  static void SetPythonEnv(const std::string &key, const std::string &value)
  {
    PyObject *os_module = PyImport_ImportModule("os");
    if (os_module == nullptr)
    {
      PyErr_Clear();
      return;
    }
    PyObject *environ = PyObject_GetAttrString(os_module, "environ");
    PyObject *py_key = PyUnicode_FromString(key.c_str());
    PyObject *py_value = PyUnicode_FromString(value.c_str());
    if (environ != nullptr && py_key != nullptr && py_value != nullptr)
    {
      if (PyObject_SetItem(environ, py_key, py_value) != 0)
      {
        PyErr_Clear();
      }
    }
    Py_XDECREF(py_key);
    Py_XDECREF(py_value);
    Py_XDECREF(environ);
    Py_DECREF(os_module);
  }

  // static
  void SeriousPythonWindowsPlugin::RegisterWithRegistrar(
      flutter::PluginRegistrarWindows *registrar)
  {
    auto channel =
        std::make_unique<flutter::MethodChannel<flutter::EncodableValue>>(
            registrar->messenger(), "serious_python_windows",
            &flutter::StandardMethodCodec::GetInstance());

    auto plugin = std::make_unique<SeriousPythonWindowsPlugin>();

    channel->SetMethodCallHandler(
        [plugin_pointer = plugin.get()](const auto &call, auto result)
        {
          plugin_pointer->HandleMethodCall(call, std::move(result));
        });

    registrar->AddPlugin(std::move(plugin));
  }

  SeriousPythonWindowsPlugin::SeriousPythonWindowsPlugin() {}

  SeriousPythonWindowsPlugin::~SeriousPythonWindowsPlugin() {}

  void SeriousPythonWindowsPlugin::HandleMethodCall(
      const flutter::MethodCall<flutter::EncodableValue> &method_call,
      std::unique_ptr<flutter::MethodResult<flutter::EncodableValue>> result)
  {
    const auto *arguments = std::get_if<EncodableMap>(method_call.arguments());

    if (method_call.method_name().compare("getPlatformVersion") == 0)
    {
      std::ostringstream version_stream;
      version_stream << "Windows ";
      if (IsWindows10OrGreater())
      {
        version_stream << "10+";
      }
      else if (IsWindows8OrGreater())
      {
        version_stream << "8";
      }
      else if (IsWindows7OrGreater())
      {
        version_stream << "7";
      }

      result->Success(flutter::EncodableValue(version_stream.str()));
    }
    else if (method_call.method_name().compare("runPython") == 0)
    {
      std::string exe_path;
      std::string app_path;
      std::string script;
      flutter::EncodableList module_paths;
      flutter::EncodableMap env_vars;
      bool sync = false;

      if (arguments)
      {
        auto exe_path_it = arguments->find(EncodableValue("exePath"));
        if (exe_path_it != arguments->end())
        {
          exe_path = std::get<std::string>(exe_path_it->second);
        }

        auto app_path_it = arguments->find(EncodableValue("appPath"));
        if (app_path_it != arguments->end())
        {
          app_path = std::get<std::string>(app_path_it->second);
        }

        auto script_it = arguments->find(EncodableValue("script"));
        if (script_it != arguments->end())
        {
          script = std::get<std::string>(script_it->second);
        }

        auto module_paths_it = arguments->find(EncodableValue("modulePaths"));
        if (module_paths_it != arguments->end() && !module_paths_it->second.IsNull())
        {
          module_paths = std::get<flutter::EncodableList>(module_paths_it->second);
        }

        auto env_vars_it = arguments->find(EncodableValue("environmentVariables"));
        if (env_vars_it != arguments->end() && !env_vars_it->second.IsNull())
        {
          env_vars = std::get<flutter::EncodableMap>(env_vars_it->second);
        }

        auto sync_it = arguments->find(EncodableValue("sync"));
        if (sync_it != arguments->end() && !sync_it->second.IsNull())
        {
          sync = std::get<bool>(sync_it->second);
        }
      }
      else
      {
        result->Error("ARGUMENT_ERROR", "arguments is missing.");
        return;
      }

      std::string exe_dir = std::filesystem::path(exe_path).parent_path().string();
      std::string app_dir = std::filesystem::path(app_path).parent_path().string();

      printf("exePath: %s\n", exe_path.c_str());
      printf("exeDir: %s\n", exe_dir.c_str());
      printf("appPath: %s\n", app_path.c_str());

      std::vector<std::string> python_paths;

      // add user module paths to the top
      for (const auto &item : module_paths)
      {
        if (auto str_value = std::get_if<std::string>(&item))
        {
          printf("module_path: %s\n", str_value->c_str());
          python_paths.push_back(*str_value);
        }
      }

      // add system paths
      python_paths.push_back(app_dir);
      python_paths.push_back(app_dir + "\\__pypackages__");
      python_paths.push_back(exe_dir + "\\site-packages");
      python_paths.push_back(exe_dir + "\\DLLs");
      python_paths.push_back(exe_dir + "\\Lib");
      python_paths.push_back(exe_dir + "\\Lib\\site-packages");

      std::string python_path;
      for (int i = 0; i < python_paths.size(); i++)
      {
        python_path += python_paths[i];
        if (i < python_paths.size() - 1)
        { // Don't add separator after the last element
          python_path += ";";
        }
      }

      printf("PYTHONPATH: %s\n", python_path.c_str());

      // Keep the UTF-8 values for the post-init sys.path/os.environ fixup.
      exe_dir_ = exe_dir;
      python_paths_ = python_paths;
      env_vars_.clear();

      // set python-related env vars through the wide CRT
      SetEnv("PYTHONUTF8", "1");
      SetEnv("PYTHONDONTWRITEBYTECODE", "1");
      SetEnv("PYTHONNOUSERSITE", "1");
      SetEnv("PYTHONUNBUFFERED", "1");
      SetEnv("LC_CTYPE", "UTF-8");
      SetEnv("PYTHONHOME", exe_dir);
      SetEnv("PYTHONPATH", python_path);

      // set user environment variables (FLET_APP_STORAGE_DATA etc.)
      for (const auto &kv : env_vars)
      {
        auto key = kv.first;
        auto value = kv.second;
        if (auto str_key = std::get_if<std::string>(&key);
            auto str_value = std::get_if<std::string>(&value))
        {
          printf("env_var: %s=%s\n", str_key->c_str(), str_value->c_str());
          env_vars_[*str_key] = *str_value;
          SetEnv(*str_key, *str_value);
        }
      }

      printf("sync: %s\n", sync ? "true" : "false");

      // run program
      if (sync)
      {
        if (script.empty())
        {
          printf("Running Python program synchronously...");
          RunPythonProgram(app_path);
        }
        else
        {
          printf("Running Python script synchronously...");
          RunPythonScript(script);
        }
      }
      else
      {
        if (script.empty())
        {
          printf("Running Python program asynchronously...");
          RunPythonProgramAsync(app_path);
        }
        else
        {
          printf("Running Python script asynchronously...");
          RunPythonScriptAsync(script);
        }
      }

      result->Success(flutter::EncodableValue(app_path));
    }
    else
    {
      result->NotImplemented();
    }
  }

  void SeriousPythonWindowsPlugin::RunPythonProgramAsync(std::string appPath)
  {
    // Create a new thread that runs the program
    std::thread pyThread(&SeriousPythonWindowsPlugin::RunPythonProgram, this, appPath);

    // Detach the thread so it runs independently
    pyThread.detach();
  }

  void SeriousPythonWindowsPlugin::RunPythonScriptAsync(std::string script)
  {
    // Create a new thread that runs the script
    std::thread pyThread(&SeriousPythonWindowsPlugin::RunPythonScript, this, script);

    // Detach the thread so it runs independently
    pyThread.detach();
  }

  void SeriousPythonWindowsPlugin::ConfigurePython()
  {
    // python_paths_ is ordered highest priority first. Insert in reverse so
    // the first entry ends up at sys.path[0].
    for (auto it = python_paths_.rbegin(); it != python_paths_.rend(); ++it)
    {
      InsertSysPath(*it, 0);
    }
    for (const auto &entry : env_vars_)
    {
      SetPythonEnv(entry.first, entry.second);
    }
  }

  void SeriousPythonWindowsPlugin::RunPythonProgram(std::string appPath)
  {
    std::wstring wide_exe_dir = Utf8ToWide(exe_dir_);
    if (!wide_exe_dir.empty())
    {
      // Py_SetPythonHome is deprecated but is the only pre-init home setter
      // available in the bundled 3.12 runtime; suppress MSVC's /WX warning.
#pragma warning(push)
#pragma warning(disable : 4996)
      Py_SetPythonHome(wide_exe_dir.c_str());
#pragma warning(pop)
    }

    Py_Initialize();
    ConfigurePython();

    std::wstring wide_app_path = Utf8ToWide(appPath);
    FILE *file = nullptr;
    errno_t err = _wfopen_s(&file, wide_app_path.c_str(), L"rb");
    if (err == 0 && file != nullptr)
    {
      size_t slash = appPath.find_last_of("\\/");
      std::string filename =
          slash == std::string::npos ? appPath : appPath.substr(slash + 1);
      PyRun_SimpleFileEx(file, filename.c_str(), 1);
      fclose(file);
    }

    Py_Finalize();
  }

  void SeriousPythonWindowsPlugin::RunPythonScript(std::string script)
  {
    std::wstring wide_exe_dir = Utf8ToWide(exe_dir_);
    if (!wide_exe_dir.empty())
    {
#pragma warning(push)
#pragma warning(disable : 4996)
      Py_SetPythonHome(wide_exe_dir.c_str());
#pragma warning(pop)
    }

    Py_Initialize();
    ConfigurePython();

    PyRun_SimpleString(script.c_str());

    Py_Finalize();
  }

} // namespace serious_python_windows
