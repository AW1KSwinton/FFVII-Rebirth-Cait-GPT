#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#ifdef CDECL
#undef CDECL
#endif

#include <Mod/CppUserModBase.hpp>
#include "persistent_sampler.hpp"
#include "sample_format.hpp"

#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace FF7RTelemetryExporter
{
    constexpr wchar_t kOutputDirectory[] = L"C:\\CaitGPT\\Telemetry";
    constexpr wchar_t kOutputPath[] = L"C:\\CaitGPT\\Telemetry\\state.json";
    constexpr wchar_t kSamplePath[] = L"C:\\CaitGPT\\Telemetry\\state.sample";

    std::atomic<bool> g_running{false};
    std::thread g_writer;
    uint64_t g_output_sequence{};
    uint64_t g_last_source_sequence{};

    bool read_complete_sample(Sample& sample)
    {
        std::ifstream file(kSamplePath, std::ios::binary);
        if (!file) return false;
        std::string line((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
        SYSTEMTIME handoff_time{};
        GetSystemTime(&handoff_time);
        return parse_complete_sample(sample, std::move(line), handoff_time, g_last_source_sequence);
    }

    void append_member(std::string& json, const Member& member)
    {
        char buffer[384]{};
        std::snprintf(buffer, sizeof(buffer),
                      "{\"name\":\"%s\",\"hp\":%d,\"max_hp\":%d,\"mp\":%d,\"max_mp\":%d,\"atb\":0.0}",
                      player_name(member.type), member.hp, member.max_hp, member.mp, member.max_mp);
        json += buffer;
    }

    void append_optional_float(std::string& json, const std::optional<float>& value)
    {
        if (!value) { json += "null"; return; }
        char buffer[64]{};
        std::snprintf(buffer, sizeof(buffer), "%.6f", *value);
        json += buffer;
    }

    void append_json_string(std::string& json, std::string_view value)
    {
        json += '"';
        for (const char character : value)
        {
            if (character == '"' || character == '\\') json += '\\';
            if (static_cast<unsigned char>(character) >= 0x20) json += character;
        }
        json += '"';
    }

    std::string build_json(const Sample* sample, std::string_view published_at_utc,
                           double producer_elapsed_ms)
    {
        const uint64_t sequence = ++g_output_sequence;
        const std::string sampled_at = sample ? sample->sampled_at_utc : utc_timestamp();
        const std::string source_timestamp = sample ? sample->source_timestamp_utc : sampled_at;
        Member player{};
        player.type = sample ? sample->leader_type : 16;
        if (sample)
        {
            for (const Member& member : sample->members)
                if (member.type == sample->leader_type) { player = member; break; }
        }

        std::string json;
        json.reserve(2300);
        json += "{\"schema_version\":1,\"sequence\":";
        json += std::to_string(sequence);
        json += ",\"source_sequence\":";
        json += sample ? std::to_string(sample->source_sequence) : "null";
        json += ",\"timestamp_utc\":\"";
        json += sampled_at;
        json += "\",\"sampled_at_utc\":\"";
        json += sampled_at;
        json += "\",\"source_timestamp_utc\":\"";
        json += source_timestamp;
        json += "\",\"published_at_utc\":\"";
        json += published_at_utc;
        json += "\",\"producer_elapsed_ms\":";
        char elapsed_text[64]{};
        std::snprintf(elapsed_text, sizeof(elapsed_text), "%.3f", producer_elapsed_ms);
        json += elapsed_text;
        json += ",\"game\":{\"running\":true,\"paused\":";
        json += sample && sample->paused ? (*sample->paused ? "true" : "false") : "false";
        json += ",\"in_menu\":";
        json += sample && sample->in_menu ? (*sample->in_menu ? "true" : "false") : "false";
        json += ",\"in_combat\":";
        json += sample && sample->in_combat ? (*sample->in_combat ? "true" : "false") : "false";
        json += ",\"combat_state_observed\":";
        json += sample && sample->in_combat.has_value() ? "true" : "false";
        json += ",\"map_id\":";
        if (sample && !sample->map_id.empty()) append_json_string(json, sample->map_id);
        else json += "null";
        json += ",\"location\":";
        if (sample && !sample->location_id.empty()) append_json_string(json, sample->location_id);
        else json += "\"\"";
        json += ",\"objective\":\"\"},\"player\":";
        append_member(json, player);
        json.pop_back();
        json += ",\"controlled_character\":";
        append_json_string(json, player_name(player.type));
        json += ",\"position\":";
        if (sample && sample->position_x)
        {
            json += "{\"x\":";
            append_optional_float(json, sample->position_x);
            json += ",\"y\":";
            append_optional_float(json, sample->position_y);
            json += ",\"z\":";
            append_optional_float(json, sample->position_z);
            json += '}';
        }
        else json += "null";
        json += ",\"rotation\":";
        if (sample && sample->rotation_pitch)
        {
            json += "{\"pitch\":";
            append_optional_float(json, sample->rotation_pitch);
            json += ",\"yaw\":";
            append_optional_float(json, sample->rotation_yaw);
            json += ",\"roll\":";
            append_optional_float(json, sample->rotation_roll);
            json += '}';
        }
        else json += "null";
        json += ",\"locomotion\":";
        if (sample && sample->locomotion.size() == 10)
        {
            const auto& pose = sample->locomotion;
            char physical[768]{};
            const char* grounded = pose[8] == 1 ? "true" : pose[8] == 0 ? "false" : "null";
            std::snprintf(physical, sizeof(physical),
                "{\"reference\":\"upright_capsule_bottom\",\"feet_position\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
                "\"capsule_radius_cm\":%.6f,\"capsule_half_height_cm\":%.6f,"
                "\"velocity_cm_s\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
                "\"grounded\":%s,\"movement_mode\":%.0f}",
                pose[0], pose[1], pose[2], pose[3], pose[4], pose[5], pose[6], pose[7], grounded, pose[9]);
            json += physical;
        }
        else json += "null";
        json += ",\"controller_state\":";
        if (sample && sample->controller_state) json += std::to_string(*sample->controller_state);
        else json += "null";
        json += ",\"actor_id\":";
        if (sample && !sample->controller_actor_id.empty())
            append_json_string(json, sample->controller_actor_id);
        else json += "null";
        json += '}';
        json += ",\"camera\":{\"rotation\":";
        if (sample && sample->camera_pitch)
        {
            json += "{\"pitch\":";
            append_optional_float(json, sample->camera_pitch);
            json += ",\"yaw\":";
            append_optional_float(json, sample->camera_yaw);
            json += ",\"roll\":";
            append_optional_float(json, sample->camera_roll);
            json += '}';
        }
        else json += "null";
        json += '}';
        json += ",\"party\":[";
        if (sample)
        {
            for (size_t index = 0; index < sample->members.size(); ++index)
            {
                if (index) json += ',';
                append_member(json, sample->members[index]);
            }
        }
        json += "],\"target\":";
        if (sample && !sample->target_id.empty() && sample->target_distance)
        {
            json += "{\"id\":";
            append_json_string(json, sample->target_id);
            json += ",\"distance\":";
            append_optional_float(json, sample->target_distance);
            json += ",\"hp\":";
            if (sample->target_hp) json += std::to_string(*sample->target_hp);
            else json += "null";
            json += ",\"max_hp\":";
            if (sample->target_max_hp) json += std::to_string(*sample->target_max_hp);
            else json += "null";
            json += '}';
        }
        else json += "null";
        json += ",\"nearby_interactable\":";
        if (sample && !sample->bench_id.empty() && sample->bench_distance && sample->bench_usable)
        {
            json += "{\"id\":";
            append_json_string(json, sample->bench_id);
            json += ",\"kind\":\"bench\",\"distance\":";
            append_optional_float(json, sample->bench_distance);
            json += ",\"usable\":";
            json += *sample->bench_usable ? "true" : "false";
            if (sample->bench_position_x && sample->bench_position_y && sample->bench_position_z)
            {
                json += ",\"position\":{\"x\":";
                append_optional_float(json, sample->bench_position_x);
                json += ",\"y\":";
                append_optional_float(json, sample->bench_position_y);
                json += ",\"z\":";
                append_optional_float(json, sample->bench_position_z);
                json += '}';
            }
            json += '}';
        }
        else json += "null";
        json += ",\"nearby_enemies\":[],\"rest\":{\"state\":";
        json += sample && !sample->bench_id.empty() ? "\"bench_detected\"" : "\"idle\"";
        json += ",\"interaction_id\":null,\"state_identity\":null},"
                "\"telemetry\":{\"live\":true}}\n";
        return json;
    }

    bool write_atomic(const std::string& json)
    {
        CreateDirectoryW(kOutputDirectory, nullptr);
        const std::wstring temporary = std::wstring(kOutputPath) + L".tmp";
        HANDLE file = CreateFileW(temporary.c_str(), GENERIC_WRITE, FILE_SHARE_READ,
                                  nullptr, CREATE_ALWAYS,
                                  FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH, nullptr);
        if (file == INVALID_HANDLE_VALUE) return false;
        DWORD written{};
        const bool ok = WriteFile(file, json.data(), static_cast<DWORD>(json.size()),
                                  &written, nullptr) &&
                        written == json.size() && FlushFileBuffers(file);
        CloseHandle(file);
        if (!ok) return false;
        return MoveFileExW(temporary.c_str(), kOutputPath,
                           MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != FALSE;
    }

    void writer_loop(PersistentSampler* sampler)
    {
        const std::string initial_published_at = utc_timestamp();
        write_atomic(build_json(nullptr, initial_published_at, 0.0));
        auto next_health = std::chrono::steady_clock::now();
        while (g_running.load(std::memory_order_acquire))
        {
            Sample sample;
            if (read_complete_sample(sample))
            {
                g_last_source_sequence = sample.source_sequence;
                const auto publication_start = std::chrono::steady_clock::now();
                const double producer_elapsed_ms = std::chrono::duration<double, std::milli>(
                    publication_start - sample.sampled_at_monotonic).count();
                const std::string published_at = utc_timestamp();
                write_atomic(build_json(&sample, published_at, producer_elapsed_ms));
            }
            if (std::chrono::steady_clock::now() >= next_health)
            {
                sampler->write_health(utc_timestamp());
                next_health = std::chrono::steady_clock::now() + std::chrono::seconds(1);
            }
            Sleep(20);
        }
    }

    class TelemetryMod : public RC::CppUserModBase
    {
        PersistentSampler sampler;
    public:
        void on_unreal_init() override { sampler.install_hooks(); }
        void on_update() override { sampler.activate_pending(); }

        void on_lua_start(RC::StringViewType name, RC::LuaMadeSimple::Lua&,
                          RC::LuaMadeSimple::Lua& main_lua, RC::LuaMadeSimple::Lua&,
                          std::vector<RC::LuaMadeSimple::Lua*>&) override
        { sampler.prepare_attach(name, main_lua); }

        void on_lua_stop(RC::StringViewType name, RC::LuaMadeSimple::Lua&,
                         RC::LuaMadeSimple::Lua&, RC::LuaMadeSimple::Lua&,
                         std::vector<RC::LuaMadeSimple::Lua*>&) override
        { sampler.detach(name); }

        TelemetryMod()
        {
            ModName = STR("FF7RTelemetryExporter");
            ModVersion = STR("2.3.0");
            ModAuthors = STR("Local FFVII Rebirth Codex integration");
            ModDescription = STR("Atomic JSON publisher for read-only game-thread telemetry");
            g_running.store(true, std::memory_order_release);
            g_writer = std::thread(writer_loop, &sampler);
        }

        ~TelemetryMod() override
        {
            g_running.store(false, std::memory_order_release);
            if (g_writer.joinable()) g_writer.join();
        }
    };
}

#define MOD_EXPORT __declspec(dllexport)
extern "C"
{
    MOD_EXPORT RC::CppUserModBase* start_mod()
    {
        return new FF7RTelemetryExporter::TelemetryMod();
    }

    MOD_EXPORT void uninstall_mod(RC::CppUserModBase* mod)
    {
        delete mod;
    }
}
