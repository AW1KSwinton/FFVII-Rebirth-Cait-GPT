#pragma once

#include <Windows.h>
#include <TlHelp32.h>
#include <limits>
#include <LuaMadeSimple/LuaMadeSimple.hpp>
#include <Unreal/Common.hpp>
#include <functional>

// Exact declaration from UEPseudo b4a6e7e (the installed c0a8d4c dependency),
// include/Unreal/Hooks.hpp. The older template omits this exported callback.
namespace RC::Unreal {
    class UGameViewportClient;
    class UObject;
    class UFunction;
    namespace Hook {
        using ProcessEventCallback = std::function<void(UObject*, UFunction*, void*)>;
        RC_UE_API auto RegisterProcessEventPreCallback(ProcessEventCallback) -> void;
        using GameViewportClientTickCallback = std::function<void(UGameViewportClient*, float)>;
        RC_UE_API auto RegisterGameViewportClientTickPreCallback(GameViewportClientTickCallback) -> void;
    }
}
#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <memory>
#include <fstream>
#include <sstream>
#include <mutex>
#include <string_view>
#include "object_path_bindings.hpp"

namespace FF7RTelemetryExporter
{
    // LuaMod::ExecuteInGameThread allocates a coroutine on each invocation.
    // The v3.0.1 LuaMadeSimple registry is shared with other threads without
    // synchronization. Reuse the mod's persistent main state instead; these
    // four paired scripts register no async callbacks or object notifications.
    class PersistentSampler
    {
        using Clock = std::chrono::steady_clock;
        struct Slot
        {
            std::wstring_view name;
            std::chrono::milliseconds period;
            RC::LuaMadeSimple::Lua* lua{};
            RC::LuaMadeSimple::Lua* pending{};
            Clock::time_point next{};
            uint64_t failures{};
            uint64_t calls{};
            std::string last_error;
            double last_duration_ms{};
            double max_duration_ms{};
            // Cumulative, so a reader can difference two health files.
            double total_duration_ms{};
            uint64_t over_frame_calls{};
        };
        struct State
        {
            std::recursive_mutex mutex;
            std::atomic<DWORD> game_thread{};
            bool alive{true};
            bool hooks_installed{};
            uint64_t update_calls{};
            uint64_t prepare_calls{};
            std::atomic<uint64_t> viewport_ticks{};
            std::atomic<uint64_t> process_events{};
            bool dispatching{};
            Clock::time_point next_dispatch{};
            // Game-thread stalls: gaps between consecutive game-thread
            // ProcessEvent calls, excluding this sampler's own callbacks
            // (the reference point is reset after them). Diagnostics only.
            Clock::time_point last_game_event{};
            uint64_t stalls_over_50ms{};
            uint64_t stalls_over_100ms{};
            uint64_t stalls_over_250ms{};
            double stall_ms_total{};
            double max_stall_ms{};
            std::array<Slot, 4> slots{{
                {L"FF7RTelemetryExporter", std::chrono::milliseconds(100)},
                {L"FF7RNavigationProbe", std::chrono::milliseconds(500)},
                {L"FF7RNavmeshPathProbe", std::chrono::milliseconds(2000)},
                {L"FF7RCustomNavAPIProbe", std::chrono::milliseconds(1000)},
            }};
        };
        std::shared_ptr<State> state = std::make_shared<State>();

        static DWORD discover_main_thread()
        {
            // Same earliest-creation-time method used by installed UE4SS in
            // main_ue4ss_rewritten.cpp::get_main_thread_id. Include the first
            // snapshot entry and close the snapshot handle as well.
            HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
            if (snapshot == INVALID_HANDLE_VALUE) return 0;
            THREADENTRY32 entry{};
            entry.dwSize = sizeof(entry);
            uint64_t earliest = std::numeric_limits<uint64_t>::max();
            DWORD candidate = 0;
            bool tied = false;
            if (Thread32First(snapshot, &entry)) do
            {
                if (entry.th32OwnerProcessID != GetCurrentProcessId()) continue;
                HANDLE thread = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, entry.th32ThreadID);
                if (!thread) continue;
                FILETIME created{}, exited{}, kernel{}, user{};
                if (GetThreadTimes(thread, &created, &exited, &kernel, &user))
                {
                    uint64_t time = (uint64_t(created.dwHighDateTime) << 32) | created.dwLowDateTime;
                    if (time < earliest) { earliest = time; candidate = entry.th32ThreadID; tied = false; }
                    else if (time == earliest) tied = true;
                }
                CloseHandle(thread);
            } while (Thread32Next(snapshot, &entry));
            CloseHandle(snapshot);
            return tied ? 0 : candidate;
        }

        static void identify_thread(const std::shared_ptr<State>& state)
        {
            // The installed build exposes the game viewport tick directly.
            // Never infer affinity from arbitrary ProcessEvent calls, which
            // may also occur on loading/worker threads.
            DWORD expected = 0;
            const DWORD current = GetCurrentThreadId();
            if (state->game_thread.compare_exchange_strong(expected, current))
                printf_s("[FF7RPersistentSampler] game thread=%lu\n", current);
            else if (expected != current)
            {
                std::lock_guard guard(state->mutex);
                state->alive = false;
                printf_s("[FF7RPersistentSampler] viewport thread conflict; sampling stopped\n");
            }
        }

        static void dispatch(const std::shared_ptr<State>& state)
        {
            if (state->game_thread.load() != GetCurrentThreadId()) return;
            std::lock_guard guard(state->mutex);
            if (!state->dispatching)
            {
                const auto event_time = Clock::now();
                if (state->last_game_event != Clock::time_point{})
                {
                    const double gap = std::chrono::duration<double, std::milli>(event_time - state->last_game_event).count();
                    if (gap > 50.0)
                    {
                        ++state->stalls_over_50ms;
                        state->stall_ms_total += gap;
                        if (gap > 100.0) ++state->stalls_over_100ms;
                        if (gap > 250.0) ++state->stalls_over_250ms;
                        state->max_stall_ms = (std::max)(state->max_stall_ms, gap);
                    }
                }
                state->last_game_event = event_time;
            }
            if (!state->alive || state->dispatching) return;
            if (Clock::now() < state->next_dispatch) return;
            state->dispatching = true;
            for (auto& slot : state->slots)
            {
                const auto now = Clock::now();
                if (!slot.lua || now < slot.next) continue;
                // No catch-up bursts after loading or a slow callback.
                slot.next = now + slot.period;
                const int top = slot.lua->get_stack_size();
                const auto started = Clock::now();
                try
                {
                    slot.lua->call_function("FF7RReadOnlyTick", 0, 0);
                    ++slot.calls;
                    if (slot.calls == 1 || slot.calls % 4000 == 0)
                        printf_s("[FF7RPersistentSampler] %ls calls=%llu thread=%lu\n",
                                 slot.name.data(), slot.calls, GetCurrentThreadId());
                }
                catch (const std::exception& error)
                {
                    ++slot.failures;
                    slot.last_error = std::string(error.what()).substr(0, 512);
                    if (slot.failures <= 3 || slot.failures % 100 == 0)
                        printf_s("[FF7RPersistentSampler] %ls failure=%llu: %s\n",
                                 slot.name.data(), slot.failures, error.what());
                }
                while (slot.lua->get_stack_size() > top) slot.lua->discard_value(-1);
                slot.last_duration_ms = std::chrono::duration<double, std::milli>(Clock::now() - started).count();
                slot.max_duration_ms = (std::max)(slot.max_duration_ms, slot.last_duration_ms);
                slot.total_duration_ms += slot.last_duration_ms;
                if (slot.last_duration_ms > 16.7) ++slot.over_frame_calls;
                // Schedule from completion: a slow callback must not already
                // be overdue when it returns and immediately run again.
                slot.next = Clock::now() + slot.period;
                if (slot.lua->get_stack_size() < top)
                {
                    // A callback must not consume its caller's stack.
                    slot.lua = nullptr;
                    printf_s("[FF7RPersistentSampler] stack imbalance; sampler detached\n");
                }
            }
            state->last_game_event = Clock::now();
            state->next_dispatch = Clock::now() + std::chrono::seconds(1);
            for (const auto& slot : state->slots)
                if (slot.lua && slot.next < state->next_dispatch) state->next_dispatch = slot.next;
            state->dispatching = false;
        }

    public:
        void install_hooks()
        {
            { std::lock_guard guard(state->mutex); state->hooks_installed = true; }
            auto shared = state;
            state->game_thread.store(discover_main_thread());
            RC::Unreal::Hook::RegisterProcessEventPreCallback(
                [shared](auto*, auto*, void*) { ++shared->process_events; dispatch(shared); });
            RC::Unreal::Hook::RegisterGameViewportClientTickPreCallback(
                [shared](auto*, float) { ++shared->viewport_ticks; identify_thread(shared); dispatch(shared); });
        }

        void prepare_attach(std::wstring_view name, RC::LuaMadeSimple::Lua& lua)
        {
            std::lock_guard guard(state->mutex);
            ++state->prepare_calls;
            if (name == L"FF7RTelemetryExporter" || name == L"FF7RNavigationProbe" ||
                name == L"FF7RNavmeshPathProbe" || name == L"FF7RCustomNavAPIProbe" ||
                name == L"FF7RQueenBloodProbe")
                register_object_path_bindings(lua, std::shared_ptr<std::atomic<DWORD>>(state, &state->game_thread));
            for (auto& slot : state->slots)
                if (slot.name == name) slot.pending = &lua;
        }

        void activate_pending()
        {
            // v3.0.1 fires on_lua_start BEFORE executing main.lua, despite its
            // header comment. UE4SSProgram::update calls on_program_start
            // (including synchronous start_lua_mods) before any mod on_update.
            // Check globals only here, on that same mod-loading thread, then
            // transfer exclusive use of the idle state to game-thread dispatch.
            std::lock_guard guard(state->mutex);
            ++state->update_calls;
            for (auto& slot : state->slots)
            {
                if (!slot.pending) continue;
                auto* lua = slot.pending;
                slot.pending = nullptr;
                if (!lua->is_global_function("FF7RReadOnlyTick"))
                {
                    printf_s("[FF7RPersistentSampler] %ls lacks paired sampler entry point\n", slot.name.data());
                    continue;
                }
                slot.lua = lua;
                slot.next = Clock::time_point{};
                state->next_dispatch = Clock::time_point{};
                printf_s("[FF7RPersistentSampler] attached %ls period=%lldms\n",
                         slot.name.data(), static_cast<long long>(slot.period.count()));
            }
        }

        void detach(std::wstring_view name)
        {
            std::lock_guard guard(state->mutex);
            for (auto& slot : state->slots)
                if (slot.name == name) { slot.lua = nullptr; slot.pending = nullptr; }
        }

        void write_health(const std::string& timestamp)
        {
            // Called by the existing publisher thread. Never wait for a stuck
            // sampler or query a game object here. Diagnostic evidence only.
            std::unique_lock guard(state->mutex, std::try_to_lock);
            std::ostringstream json;
            json << "{\"schema_version\":1,\"timestamp_utc\":\"" << timestamp
                 << "\",\"object_path_resolver_version\":1"
                 << ",\"pid\":" << GetCurrentProcessId()
                 << ",\"game_thread\":" << state->game_thread.load()
                 << ",\"thread_source\":\"ue4ss_main_thread_discovery\""
                 << ",\"process_events\":" << state->process_events.load()
                 << ",\"viewport_ticks\":" << state->viewport_ticks.load()
                 << ",\"sampler_busy\":" << (guard.owns_lock() ? "false" : "true");
            if (const auto paths = object_path_context.load())
                json << ",\"object_path_weak_hits\":" << paths->weak_hits.load()
                     << ",\"object_path_searches_allowed\":" << paths->searches_allowed.load();
            if (guard.owns_lock())
            {
                json << ",\"hooks_installed\":" << (state->hooks_installed ? "true" : "false")
                     << ",\"alive\":" << (state->alive ? "true" : "false")
                     << ",\"update_calls\":" << state->update_calls
                     << ",\"prepare_calls\":" << state->prepare_calls
                     << ",\"stalls\":{\"over_50ms\":" << state->stalls_over_50ms
                     << ",\"over_100ms\":" << state->stalls_over_100ms
                     << ",\"over_250ms\":" << state->stalls_over_250ms
                     << ",\"total_ms\":" << state->stall_ms_total
                     << ",\"max_ms\":" << state->max_stall_ms << '}'
                     << ",\"slots\":[";
                bool first = true;
                for (const auto& slot : state->slots)
                {
                    if (!first) json << ',';
                    first = false;
                    std::string ascii_name;
                    for (wchar_t ch : slot.name) ascii_name.push_back(static_cast<char>(ch));
                    json << "{\"name\":\"" << ascii_name
                         << "\",\"attached\":" << (slot.lua ? "true" : "false")
                         << ",\"pending\":" << (slot.pending ? "true" : "false")
                         << ",\"calls\":" << slot.calls << ",\"failures\":" << slot.failures
                         << ",\"last_duration_ms\":" << slot.last_duration_ms
                         << ",\"max_duration_ms\":" << slot.max_duration_ms
                         << ",\"total_duration_ms\":" << slot.total_duration_ms
                         << ",\"over_frame_calls\":" << slot.over_frame_calls
                         << ",\"last_error\":\"";
                    for (const unsigned char ch : slot.last_error)
                    {
                        if (ch == '\\' || ch == '"') json << '\\';
                        json << static_cast<char>(ch < 32 ? ' ' : ch);
                    }
                    json << "\"}";
                }
                json << ']';
            }
            json << "}\n";
            const wchar_t* temporary = L"C:\\CaitGPT\\Telemetry\\persistent-sampler-health.json.tmp";
            const wchar_t* destination = L"C:\\CaitGPT\\Telemetry\\persistent-sampler-health.json";
            std::ofstream file(temporary, std::ios::binary | std::ios::trunc);
            file << json.str();
            file.flush();
            const bool ok = file.good();
            file.close();
            if (ok) MoveFileExW(temporary, destination, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
        }

        ~PersistentSampler()
        {
            std::lock_guard guard(state->mutex);
            state->alive = false;
            for (auto& slot : state->slots) { slot.lua = nullptr; slot.pending = nullptr; }
        }
    };
}
