#ifndef FLUTTER_PLUGIN_SERIOUS_PYTHON_WINDOWS_PLUGIN_H_
#define FLUTTER_PLUGIN_SERIOUS_PYTHON_WINDOWS_PLUGIN_H_

#include <flutter/method_channel.h>
#include <flutter/plugin_registrar_windows.h>

#include <map>
#include <memory>
#include <string>
#include <vector>

namespace serious_python_windows
{

    class SeriousPythonWindowsPlugin : public flutter::Plugin
    {
    public:
        static void RegisterWithRegistrar(flutter::PluginRegistrarWindows *registrar);

        SeriousPythonWindowsPlugin();

        virtual ~SeriousPythonWindowsPlugin();

        // Disallow copy and assign.
        SeriousPythonWindowsPlugin(const SeriousPythonWindowsPlugin &) = delete;
        SeriousPythonWindowsPlugin &operator=(const SeriousPythonWindowsPlugin &) = delete;

        // Called when a method is called on this plugin's channel from Dart.
        void HandleMethodCall(
            const flutter::MethodCall<flutter::EncodableValue> &method_call,
            std::unique_ptr<flutter::MethodResult<flutter::EncodableValue>> result);

        void RunPythonProgram(std::string appPath);
        void RunPythonProgramAsync(std::string appPath);

        void RunPythonScript(std::string script);
        void RunPythonScriptAsync(std::string script);

    private:
        // Python's Windows bootstrap historically pushed these through the
        // narrow CRT (_putenv_s/fopen_s), which corrupts any path containing
        // non-ASCII characters (e.g. an Arabic Windows user profile). Keep the
        // UTF-8 values here and apply them through the wide-char APIs after
        // Py_Initialize instead.
        void ConfigurePython();

        std::string exe_dir_;
        std::vector<std::string> python_paths_;
        std::map<std::string, std::string> env_vars_;
    };

} // namespace serious_python_windows

#endif // FLUTTER_PLUGIN_SERIOUS_PYTHON_WINDOWS_PLUGIN_H_
