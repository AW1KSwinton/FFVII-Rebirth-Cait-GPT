#pragma once
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <iterator>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

// Shared by the installed publisher and executable parser regression tests.
// No UE4SS/game symbols, filesystem reads, or input actions occur here.
namespace FF7RTelemetryExporter
{
    struct Member
    {
        uint8_t type{16};
        int32_t hp{};
        int32_t max_hp{};
        int32_t mp{};
        int32_t max_mp{};
    };

    struct Sample
    {
        uint64_t source_sequence{};
        // timestamp_utc is the precise time at which the native publisher
        // accepted the complete game-thread snapshot.  The Lua producer's
        // original whole-second value is retained for forensic comparison.
        std::string sampled_at_utc;
        std::string source_timestamp_utc;
        std::chrono::steady_clock::time_point sampled_at_monotonic{};
        uint8_t leader_type{16};
        std::vector<Member> members;
        std::optional<float> position_x;
        std::optional<float> position_y;
        std::optional<float> position_z;
        std::optional<float> rotation_pitch;
        std::optional<float> rotation_yaw;
        std::optional<float> rotation_roll;
        std::string map_id;
        std::string location_id;
        std::optional<bool> in_combat;
        std::optional<bool> paused;
        std::optional<bool> in_menu;
        std::string target_id;
        std::optional<float> target_distance;
        std::optional<int32_t> target_hp;
        std::optional<int32_t> target_max_hp;
        std::string bench_id;
        std::optional<float> bench_distance;
        std::optional<bool> bench_usable;
        std::optional<float> bench_position_x;
        std::optional<float> bench_position_y;
        std::optional<float> bench_position_z;
        std::optional<uint8_t> controller_state;
        std::string controller_actor_id;
        std::optional<float> camera_pitch;
        std::optional<float> camera_yaw;
        std::optional<float> camera_roll;
        std::vector<float> locomotion;
    };

    const char* player_name(uint8_t type)
    {
        static constexpr const char* names[] = {
            "Cloud", "Barret", "Tifa", "Aerith", "Red XIII", "Yuffie", "Cait Sith",
            "Zack", "Sephiroth", "Vincent", "Cid", "Aerith (Age 7)",
            "PlayerType_12", "PlayerType_13", "PlayerType_14", "PlayerType_15", "None"
        };
        return type < std::size(names) ? names[type] : "Unknown";
    }

    std::string utc_timestamp(const SYSTEMTIME& now)
    {
        char text[40]{};
        std::snprintf(text, sizeof(text), "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ",
                      now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute,
                      now.wSecond, now.wMilliseconds);
        return text;
    }

    std::string utc_timestamp()
    {
        SYSTEMTIME now{};
        GetSystemTime(&now);
        return utc_timestamp(now);
    }

    template <typename T>
    bool parse_integer(std::string_view text, T& output)
    {
        if (text.empty()) return false;
        const char* begin = text.data();
        const char* end = begin + text.size();
        auto [next, error] = std::from_chars(begin, end, output);
        return error == std::errc{} && next == end;
    }

    bool source_timestamp_is_recent(std::string_view text, const SYSTEMTIME& now)
    {
        // The Lua exporter writes UTC timestamps at whole-second precision.
        // Reject malformed values and samples outside the two-second handoff
        // envelope so a native writer restart cannot relabel an old sample as
        // fresh telemetry.
        if (text.size() != 20 || text[4] != '-' || text[7] != '-' ||
            text[10] != 'T' || text[13] != ':' || text[16] != ':' ||
            text[19] != 'Z')
            return false;
        constexpr size_t starts[] = {0, 5, 8, 11, 14, 17};
        constexpr size_t lengths[] = {4, 2, 2, 2, 2, 2};
        uint32_t values[std::size(starts)]{};
        for (size_t index = 0; index < std::size(starts); ++index)
        {
            if (!parse_integer(text.substr(starts[index], lengths[index]), values[index]))
                return false;
        }
        SYSTEMTIME source{};
        source.wYear = static_cast<WORD>(values[0]);
        source.wMonth = static_cast<WORD>(values[1]);
        source.wDay = static_cast<WORD>(values[2]);
        source.wHour = static_cast<WORD>(values[3]);
        source.wMinute = static_cast<WORD>(values[4]);
        source.wSecond = static_cast<WORD>(values[5]);
        FILETIME source_file{}, now_file{};
        if (!SystemTimeToFileTime(&source, &source_file) ||
            !SystemTimeToFileTime(&now, &now_file))
            return false;
        ULARGE_INTEGER source_ticks{};
        source_ticks.LowPart = source_file.dwLowDateTime;
        source_ticks.HighPart = source_file.dwHighDateTime;
        ULARGE_INTEGER now_ticks{};
        now_ticks.LowPart = now_file.dwLowDateTime;
        now_ticks.HighPart = now_file.dwHighDateTime;
        const ULONGLONG delta = source_ticks.QuadPart >= now_ticks.QuadPart
            ? source_ticks.QuadPart - now_ticks.QuadPart
            : now_ticks.QuadPart - source_ticks.QuadPart;
        return delta <= 2ULL * 10'000'000ULL;
    }

    std::vector<std::string_view> split(std::string_view text, char delimiter)
    {
        std::vector<std::string_view> parts;
        size_t start{};
        for (;;)
        {
            const size_t position = text.find(delimiter, start);
            if (position == std::string_view::npos)
            {
                parts.emplace_back(text.substr(start));
                return parts;
            }
            parts.emplace_back(text.substr(start, position - start));
            start = position + 1;
        }
    }

    bool parse_complete_sample(Sample& sample, std::string line,
                               const SYSTEMTIME& handoff_time, uint64_t last_source_sequence)
    {
        if (line.empty() || line.back() != '\n' || line.size() > 8192) return false;
        line.pop_back();
        if (!line.empty() && line.back() == '\r') line.pop_back();
        // The line is complete and has already been produced by the game
        // thread before this timestamp is taken.  This is an ingestion/sample
        // handoff timestamp, never a file mtime or publication timestamp.
        const std::string sampled_at_utc = utc_timestamp(handoff_time);

        const auto fields = split(line, '|');
        if (fields.size() < 5 || fields.size() > 17) return false;
        uint32_t member_count{};
        uint32_t leader{};
        if (!parse_integer(fields[0], sample.source_sequence) ||
            sample.source_sequence == last_source_sequence ||
            fields[1].size() < 20 || fields[1].size() > 40 ||
            !source_timestamp_is_recent(fields[1], handoff_time) ||
            !parse_integer(fields[2], leader) || leader > 16 ||
            !parse_integer(fields[3], member_count) || member_count > 16)
            return false;

        sample.sampled_at_utc = sampled_at_utc;
        sample.source_timestamp_utc.assign(fields[1]);
        sample.sampled_at_monotonic = std::chrono::steady_clock::now();
        sample.leader_type = static_cast<uint8_t>(leader);
        sample.members.clear();
        if (member_count == 0)
        {
            if (!fields[4].empty()) return false;
        }
        else
        {
            const auto encoded_members = split(fields[4], ';');
            if (encoded_members.size() != member_count) return false;
            for (const auto encoded : encoded_members)
            {
                const auto values = split(encoded, ',');
                if (values.size() != 5) return false;
                uint32_t type{};
                Member member{};
                if (!parse_integer(values[0], type) || type > 16 ||
                    !parse_integer(values[1], member.hp) || member.hp < 0 ||
                    !parse_integer(values[2], member.max_hp) || member.max_hp < 0 ||
                    !parse_integer(values[3], member.mp) || member.mp < 0 ||
                    !parse_integer(values[4], member.max_mp) || member.max_mp < 0)
                    return false;
                member.type = static_cast<uint8_t>(type);
                sample.members.emplace_back(member);
            }
        }

        if (fields.size() >= 6 && !fields[5].empty())
        {
            const auto values = split(fields[5], ',');
            float x{}, y{}, z{}, pitch{}, yaw{}, roll{};
            if (values.size() != 6 ||
                !parse_integer(values[0], x) || !parse_integer(values[1], y) ||
                !parse_integer(values[2], z) || !parse_integer(values[3], pitch) ||
                !parse_integer(values[4], yaw) || !parse_integer(values[5], roll))
                return false;
            sample.position_x = x; sample.position_y = y; sample.position_z = z;
            sample.rotation_pitch = pitch; sample.rotation_yaw = yaw; sample.rotation_roll = roll;
        }
        if (fields.size() >= 7) sample.map_id.assign(fields[6]);
        if (fields.size() >= 8) sample.location_id.assign(fields[7]);
        if (fields.size() >= 9)
        {
            int32_t value{};
            if (!parse_integer(fields[8], value) || value < -1 || value > 1) return false;
            if (value >= 0) sample.in_combat = value != 0;
        }
        if (fields.size() >= 10)
        {
            int32_t value{};
            if (!parse_integer(fields[9], value) || value < -1 || value > 1) return false;
            if (value >= 0) sample.paused = value != 0;
        }
        if (fields.size() >= 11 && !fields[10].empty())
        {
            const auto values = split(fields[10], ',');
            float distance{};
            int32_t hp{};
            int32_t max_hp{};
            if ((values.size() != 2 && values.size() != 4) || values[0].empty() ||
                !parse_integer(values[1], distance) || distance < 0.0f)
                return false;
            sample.target_id.assign(values[0]);
            sample.target_distance = distance;
            if (values.size() == 4)
            {
                if (!parse_integer(values[2], hp) || hp < 0 ||
                    !parse_integer(values[3], max_hp) || max_hp < hp)
                    return false;
                sample.target_hp = hp;
                sample.target_max_hp = max_hp;
            }
        }
        if (fields.size() >= 12)
        {
            int32_t value{};
            if (!parse_integer(fields[11], value) || value < -1 || value > 1) return false;
            if (value >= 0) sample.in_menu = value != 0;
        }
        if (fields.size() >= 13 && !fields[12].empty())
        {
            const auto values = split(fields[12], ',');
            float distance{};
            int32_t usable{};
            if ((values.size() != 3 && values.size() != 6) || values[0].empty() ||
                !parse_integer(values[1], distance) || distance < 0.0f ||
                !parse_integer(values[2], usable) || usable < 0 || usable > 1)
                return false;
            sample.bench_id.assign(values[0]);
            sample.bench_distance = distance;
            sample.bench_usable = usable != 0;
            if (values.size() == 6)
            {
                float x{}, y{}, z{};
                if (!parse_integer(values[3], x) || !parse_integer(values[4], y) ||
                    !parse_integer(values[5], z))
                    return false;
                sample.bench_position_x = x;
                sample.bench_position_y = y;
                sample.bench_position_z = z;
            }
        }
        if (fields.size() >= 14)
        {
            int32_t value{};
            if (!parse_integer(fields[13], value) || value < -1 || value > 39) return false;
            if (value >= 0) sample.controller_state = static_cast<uint8_t>(value);
        }
        if (fields.size() >= 15)
        {
            if (fields[14].size() > 1024) return false;
            sample.controller_actor_id.assign(fields[14]);
        }
        if (fields.size() >= 16 && !fields[15].empty())
        {
            const auto values = split(fields[15], ',');
            float pitch{}, yaw{}, roll{};
            if (values.size() != 3 || !parse_integer(values[0], pitch) ||
                !parse_integer(values[1], yaw) || !parse_integer(values[2], roll))
                return false;
            sample.camera_pitch = pitch;
            sample.camera_yaw = yaw;
            sample.camera_roll = roll;
        }
        if (fields.size() >= 17 && !fields[16].empty())
        {
            const auto values = split(fields[16], ',');
            if (values.size() != 10) return false;
            for (const auto value : values)
            {
                float number{};
                if (!parse_integer(value, number) || !std::isfinite(number)) return false;
                sample.locomotion.push_back(number);
            }
            const auto& pose = sample.locomotion;
            if (pose[3] <= 0 || pose[3] > 200 || pose[4] < pose[3] || pose[4] > 300 ||
                (pose[8] != -1 && pose[8] != 0 && pose[8] != 1) ||
                pose[9] < -1 || pose[9] > 255 || std::floor(pose[9]) != pose[9]) return false;
        }
        return true;
    }

}
