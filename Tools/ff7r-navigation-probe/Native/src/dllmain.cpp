#include <Mod/CppUserModBase.hpp>
#include <Unreal/AActor.hpp>
#include <Unreal/FWeakObjectPtr.hpp>
#include <Unreal/FProperty.hpp>
#include <Unreal/UFunction.hpp>
#include <Unreal/UObjectGlobals.hpp>
#include <Unreal/UObject.hpp>

#include <Windows.h>
#include <bcrypt.h>

#include <array>
#include <chrono>
#include <cwctype>
#include <cmath>
#include <cstring>
#include <cstdint>
#include <algorithm>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <regex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#pragma comment(lib, "bcrypt.lib")

namespace
{
    constexpr std::uintptr_t EngineGameViewportOffset = 0x7E0;
    constexpr std::uintptr_t GameViewportWorldOffset = 0x80;
    constexpr std::uintptr_t ControllerPawnOffset = 0x258;
    constexpr std::uintptr_t WorldNavigationSystemOffset = 0x140;
    // Verified from the UE4SS UE4.26 CXX dump for this shipping build.
    constexpr std::uintptr_t WorldPersistentLevelOffset = 0x30;
    constexpr std::uintptr_t WorldLoadedLevelsOffset = 0x168;
    // UE4.26 Engine/Classes/Engine/Level.h places ULevel::Actors before
    // ActorsForGC and OwningWorld. The source-derived 0x98 offset is also
    // consistent with the verified OwningWorld +0xB8 layout below. This is
    // intentionally a direct array read; no actor iterator or FindAllOf is
    // used in the live probe.
    constexpr std::uintptr_t LevelActorsOffset = 0x98;
    constexpr std::uintptr_t LevelActorClusterOffset = 0xD8;
    constexpr std::uintptr_t ActorClusterActorsOffset = 0x28;
    constexpr std::uintptr_t UObjectClassOffset = 0x10;
    constexpr std::uintptr_t NavigationMainNavDataOffset = 0x28;
    constexpr std::uintptr_t NavigationDataFindPathOffset = 0x2C0;
    // Derived from the shipping layout scan: the larger Windows critical
    // section shifts DefaultQueryFilter to 0x390 in this build. It is never
    // used to authorise movement.
    constexpr std::uintptr_t NavigationDataDefaultFilterOffset = 0x390;
    constexpr std::uintptr_t NavigationDataSupportedAreasOffset = 0x3F0;
    // The shipping polygon/link helper reconstructs references with
    // tileIndex << polyBits and salt << (tileBits + polyBits). Its receiver
    // is the live dtNavMesh object; the extended build stores the two shift
    // counts at +0xC0/+0xC4 (not at the stock source tail offsets).
    constexpr std::uintptr_t DetourNavMeshTileBitsOffset = 0xC0;
    constexpr std::uintptr_t DetourNavMeshPolyBitsOffset = 0xC4;
    constexpr std::uintptr_t DtMeshTileStride = 0xC0;
    // Source-derived private ARecastNavMesh fields. These are read for
    // diagnostics only; no Recast/Detour method is called through them.
    constexpr std::uintptr_t RecastNavMeshVersionOffset = 0x4EC;
    constexpr std::uintptr_t RecastNavMeshImplOffset = 0x4F0;
    // FPImplRecastNavMesh is not polymorphic in the recovered UE4.26 source:
    // NavMeshOwner and DetourNavMesh are its first two data members.
    constexpr std::uintptr_t RecastNavMeshImplOwnerOffset = 0x00;
    constexpr std::uintptr_t RecastNavMeshImplDetourOffset = 0x08;
    constexpr ULONGLONG ProbePeriodMs = 2000;
    constexpr std::uint32_t MaximumProbeAttempts = 120;
    // About one second of rename retries while the harness holds a read.
    constexpr int MoveFileRetryAttempts = 40;
    constexpr DWORD MoveFileRetrySleepMs = 25;
    constexpr std::uint32_t DetourNavMeshMagic = 0x444E4156;
    constexpr std::int32_t DetourNavMeshVersion = 7;
    constexpr std::uint8_t DetourPolyTypeGround = 0;
    constexpr std::uint8_t DetourPolyTypeOffMeshPoint = 1;
    constexpr std::uint8_t DetourPolyTypeOffMeshSegment = 2;
    // Rebirth's shipping navmesh serializer writes version 13 into the
    // dtMeshHeader while retaining the standard DNAV header layout. This was
    // confirmed from live, settled-gameplay tile headers; accepting stock 7
    // as well keeps the diagnostic usable against an unmodified UE4.26 mesh.
    constexpr std::int32_t RebirthDetourNavMeshVersion = 13;
    // Source-backed from FF7RebirthEngineV4/Engine/Source/Runtime/Navmesh/
    // Public/Detour/DetourNavMesh.h: dtMeshTile stores salt, linksFreeList,
    // eleven pointer/data fields, dataSize, flags, then dtMeshTile* next.
    // The recovered 64-bit layout places next at 0x60. It is the link used
    // by dtNavMesh::m_posLookup bucket chains.
    constexpr std::uintptr_t DtMeshTileNextOffset = 0x60;
    // The shipping binary's link-allocation and link-resolution code proves
    // the extended dtMeshTile tail for this executable: dynamicLinksO begins
    // at +0xA0, and dynamicFreeListO is at +0xB0. No alternate dynamic-link
    // offset is used by this parser.
    constexpr std::uintptr_t DtMeshTileDynamicLinksOOffset = 0xA0;
    constexpr std::uintptr_t DtMeshTileDynamicFreeListOOffset = 0xB0;
    constexpr std::uintptr_t DtChunkArrayDataOffset = 0x00;
    constexpr std::uintptr_t DtChunkArraySizeOffset = 0x08;
    constexpr std::uintptr_t DtChunkArrayCapacityOffset = 0x0C;
    constexpr std::uintptr_t DtMeshTileDataOffset = 0x50;
    constexpr std::uintptr_t DtMeshTileDataSizeOffset = 0x58;
    // Source-derived from the recovered build with segment and cluster fields:
    // dtMeshHeader ends after bvQuantFactor at 0x74, not at the stock
    // point-link-only 0x40 boundary.
    constexpr std::uintptr_t DtMeshHeaderStride = 0x74;
    constexpr std::uintptr_t DtRecastVertexStride = 0x0C;
    constexpr std::uintptr_t DtPolyStride = 0x20;
    // The shipping executable contains repeated indexed accesses of the form
    // polys + poly_index * 0x248, followed by source-dtPoly field offsets
    // (+0x00/+0x04/+0x10/+0x1c/+0x1e/+0x1f). 0x248 is therefore the proven
    // shipping polygon-record/block stride. It is not sizeof(source dtPoly,
    // which remains 0x20); the additional 0x228 bytes are intentionally not
    // interpreted here.
    constexpr std::uintptr_t ShippingPolyRecordStride = 0x248;
    constexpr bool ShippingBinaryLayoutProven = true;
    constexpr std::uintptr_t DtLinkStride = 0x10;
    constexpr std::int32_t MaxDynamicLinkEntries = 1000000;
    constexpr std::size_t MaxMemoryDiagnosticTiles = 8;
    constexpr std::size_t MaxMemoryDiagnosticPolygons = 4;
    constexpr std::size_t MaxDynamicLinkPlausibilityDiagnosticSamples = 64;
    constexpr std::size_t MaxDiagnosticLinksPerTile = 65536;
    constexpr std::size_t MaxDiagnosticPolygonsPerTile = 50000;
    constexpr std::size_t MaxDiagnosticSamplesPerReason = 8;
    constexpr std::size_t PolygonDiagnosticWindowBytes = 0x80;
    constexpr std::int32_t MaxLookupChainLength = 4096;
    constexpr std::int32_t MaxLookupTilesVisited = 65536;
    constexpr char KnownGameSha256[] = "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7";

    constexpr wchar_t OutputPath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-probe.json";
    constexpr wchar_t EnablePath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-query.enable";
    constexpr wchar_t GeometryEnablePath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-geometry.enable";
    // This separate one-shot readiness token is written only after the
    // Python live-route gate has both fresh gameplay telemetry and positive
    // rendered-gameplay evidence. A pending geometry marker without this
    // token must stay cheap and dormant.
    constexpr wchar_t GeometryReadyPath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-geometry.ready";
    constexpr wchar_t GeometryOutputPath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-geometry.json";
    constexpr wchar_t TargetPath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-target.json";
    constexpr wchar_t StatePath[] = L"C:\\CaitGPT\\Telemetry\\state.json";
    constexpr wchar_t AuthoredRouteOutputPath[] = L"C:\\CaitGPT\\Telemetry\\authored-route-inventory.json";
    constexpr wchar_t TimingOutputPath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-probe-timing.json";
    constexpr wchar_t GeometryStatusPath[] = L"C:\\CaitGPT\\Telemetry\\navigation-native-geometry-status.json";

    struct CaptureRequest
    {
        std::string request_id;
        std::string requested_utc;
        std::string session_id;
        std::string world_id;
        std::string map_id;
        std::string executable_sha256;
        std::string native_probe_sha256;
        std::string purpose;
        bool valid{false};
    };

    CaptureRequest ActiveCaptureRequest{};
    std::chrono::steady_clock::time_point ActiveCaptureStarted{};
    bool ActiveCaptureStatusWritten{false};

    struct ProbeTiming
    {
        std::string request_id;
        long long full_build_report_us{};
        long long geometry_extraction_and_json_serialization_us{};
        long long geometry_json_write_us{};
        long long probe_report_json_serialization_us{};
        long long probe_json_write_us{};
        std::uint32_t probe_attempt{};
        bool geometry_armed{};
        bool geometry_dump_written{};
        bool geometry_export_complete{};
    };

    ProbeTiming LastProbeTiming{};

    struct Vec3
    {
        float x{};
        float y{};
        float z{};
    };

    struct RawSharedPtr
    {
        void* object{};
        void* reference_controller{};

        RawSharedPtr() = default;
        RawSharedPtr(const RawSharedPtr&) = default;
        RawSharedPtr& operator=(const RawSharedPtr&) = default;
        // Deliberately do not release the engine-owned references. This probe
        // performs at most one opt-in query per process and must not call an
        // unknown shipping-build deleter through a guessed ABI.
        ~RawSharedPtr() {}
    };

    struct RawWeakObjectPtr
    {
        std::int32_t object_index{};
        std::int32_t object_serial_number{};
    };

    struct RawTArray
    {
        void* data{};
        std::int32_t count{};
        std::int32_t capacity{};
    };

    struct RawNavAgentProperties
    {
        std::uint8_t movement_flags{};
        std::uint8_t padding[3]{};
        float agent_radius{-1.0f};
        float agent_height{-1.0f};
        float agent_step_height{-1.0f};
        float nav_walking_search_height_scale{0.5f};
        // Rebirth's reflected shipping layout includes AgentSlope at 0x14,
        // even though the recovered stock UE4.26 header omits it.
        float agent_slope{0.0f};
        std::uint8_t preferred_nav_data[0x18]{};
    };

    struct RawPathFindingQuery
    {
        Unreal::FWeakObjectPtr owner{};
        Vec3 start_location{};
        Vec3 end_location{};
        RawSharedPtr query_filter{};
        float cost_limit{(std::numeric_limits<float>::max)()};
        std::int32_t nav_data_flags{};
        std::uint32_t allow_partial_paths{1};
        Unreal::FWeakObjectPtr nav_data{};
        RawSharedPtr path_instance_to_fill{};
        RawNavAgentProperties nav_agent_properties{};
    };

    struct RawPathFindingResult
    {
        RawSharedPtr path{};
        std::uint8_t result{};
        std::uint8_t padding[7]{};
    };

    static_assert(sizeof(RawSharedPtr) == 0x10);
    static_assert(sizeof(RawWeakObjectPtr) == 0x08);
    static_assert(sizeof(RawNavAgentProperties) == 0x30);
    static_assert(sizeof(RawPathFindingQuery) == 0x88);
    static_assert(sizeof(RawPathFindingResult) == 0x18);

    struct Target
    {
        std::string id;
        Vec3 position{};
        bool has_start_position{false};
        Vec3 start_position{};
    };

    struct PathQueryOutput
    {
        bool called{false};
        std::string json;
    };

    struct ExecutableIdentity
    {
        std::wstring path;
        std::string sha256;
        bool filename_matches{false};
        bool sha256_matches{false};

        bool valid() const
        {
            return filename_matches && sha256_matches;
        }
    };

    // VirtualQuery works out a region's extent by walking its page table
    // entries, so on the game's large heap regions one call costs hundreds of
    // microseconds, and the geometry export (about two million field reads,
    // each checked) took eleven minutes (live 2026-09-30). While a
    // ReadableRegionCache is open on a thread, the readable regions it has
    // queried are remembered and reads inside them skip the query. Copies go
    // through GuardedCopy, so memory released meanwhile fails the read
    // instead of faulting the game.
    struct ReadableRegion
    {
        std::uintptr_t start{};
        std::uintptr_t end{};
    };

    struct ReadableRegionCacheState
    {
        bool open{false};
        std::array<ReadableRegion, 32> regions{};
        std::size_t next{};
    };

    thread_local ReadableRegionCacheState RegionCache;

    class ReadableRegionCache
    {
    public:
        ReadableRegionCache() : owner_(!RegionCache.open)
        {
            if (owner_)
            {
                RegionCache = {};
                RegionCache.open = true;
            }
        }
        ~ReadableRegionCache() { End(); }
        ReadableRegionCache(const ReadableRegionCache&) = delete;
        ReadableRegionCache& operator=(const ReadableRegionCache&) = delete;

        void End()
        {
            if (owner_)
            {
                RegionCache = {};
                owner_ = false;
            }
        }

    private:
        bool owner_;
    };

    bool GuardedCopy(void* destination, const void* source, std::size_t size)
    {
        __try
        {
            std::memcpy(destination, source, size);
            return true;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            return false;
        }
    }

    bool IsReadable(const void* address, std::size_t size = sizeof(void*))
    {
        if (address == nullptr)
        {
            return false;
        }
        const auto start = reinterpret_cast<std::uintptr_t>(address);
        const auto end = start + size;
        if (end < start)
        {
            return false;
        }
        if (RegionCache.open)
        {
            for (const auto& region : RegionCache.regions)
            {
                if (start >= region.start && end <= region.end)
                {
                    return true;
                }
            }
        }
        MEMORY_BASIC_INFORMATION info{};
        if (VirtualQuery(address, &info, sizeof(info)) != sizeof(info))
        {
            return false;
        }
        if (info.State != MEM_COMMIT || (info.Protect & PAGE_GUARD) != 0 || (info.Protect & PAGE_NOACCESS) != 0)
        {
            return false;
        }
        const auto region_start = reinterpret_cast<std::uintptr_t>(info.BaseAddress);
        const auto region_end = region_start + info.RegionSize;
        if (RegionCache.open)
        {
            RegionCache.regions[RegionCache.next] = {region_start, region_end};
            RegionCache.next = (RegionCache.next + 1) % RegionCache.regions.size();
        }
        return end <= region_end;
    }

    bool RangeWithin(const void* base, std::size_t base_size, const void* candidate,
        std::size_t candidate_size)
    {
        const auto base_address = reinterpret_cast<std::uintptr_t>(base);
        const auto candidate_address = reinterpret_cast<std::uintptr_t>(candidate);
        if (base_address == 0 || candidate_address == 0 ||
            base_size > (std::numeric_limits<std::uintptr_t>::max)() - base_address ||
            candidate_size > (std::numeric_limits<std::uintptr_t>::max)() - candidate_address)
        {
            return false;
        }
        const auto base_end = base_address + base_size;
        const auto candidate_end = candidate_address + candidate_size;
        return candidate_address >= base_address && candidate_end <= base_end;
    }

    bool IsExecutable(const void* address)
    {
        if (!IsReadable(address, 1))
        {
            return false;
        }
        MEMORY_BASIC_INFORMATION info{};
        if (VirtualQuery(address, &info, sizeof(info)) != sizeof(info))
        {
            return false;
        }
        const auto protection = info.Protect & 0xFF;
        return protection == PAGE_EXECUTE || protection == PAGE_EXECUTE_READ ||
               protection == PAGE_EXECUTE_READWRITE || protection == PAGE_EXECUTE_WRITECOPY;
    }

    template <typename T>
    bool ReadValue(const void* base, std::uintptr_t offset, T& out)
    {
        const auto* address = static_cast<const std::uint8_t*>(base) + offset;
        if (!IsReadable(address, sizeof(T)))
        {
            return false;
        }
        return GuardedCopy(&out, address, sizeof(T));
    }

    struct ChunkArrayObservation
    {
        void* data{};
        std::int32_t size{};
        std::int32_t capacity{};
        bool fields_read{false};
        bool structurally_valid{false};
    };

    ChunkArrayObservation ReadChunkArray(const void* tile, std::uintptr_t offset)
    {
        ChunkArrayObservation result;
        result.fields_read =
            ReadValue(tile, offset + DtChunkArrayDataOffset, result.data) &&
            ReadValue(tile, offset + DtChunkArraySizeOffset, result.size) &&
            ReadValue(tile, offset + DtChunkArrayCapacityOffset, result.capacity);
        result.structurally_valid = result.fields_read && result.size >= 0 &&
            result.capacity >= result.size && result.capacity <= MaxDynamicLinkEntries;
        return result;
    }

    struct DtPolyPrefix
    {
        std::uint32_t first_link{0xffffffff};
        std::array<std::uint16_t, 6> vertices{};
        std::array<std::uint16_t, 6> neighbors{};
        std::uint16_t flags{};
        std::uint8_t vertex_count{};
        std::uint8_t area_and_type{};
        bool fields_read{false};
    };

    DtPolyPrefix ReadDtPolyPrefix(const void* polys, std::int32_t poly_index,
        std::uintptr_t record_stride)
    {
        const auto* address = static_cast<const std::uint8_t*>(polys) +
            static_cast<std::uintptr_t>(poly_index) * record_stride;
        DtPolyPrefix result;
        result.fields_read =
            ReadValue(address, 0x00, result.first_link) &&
            ReadValue(address, 0x04, result.vertices) &&
            ReadValue(address, 0x10, result.neighbors) &&
            ReadValue(address, 0x1C, result.flags) &&
            ReadValue(address, 0x1E, result.vertex_count) &&
            ReadValue(address, 0x1F, result.area_and_type);
        return result;
    }

    bool IsValidDtPolyPrefix(const DtPolyPrefix& prefix, std::int32_t vert_count,
        std::int32_t max_link_count, const ChunkArrayObservation& dynamic_links)
    {
        if (!prefix.fields_read || prefix.flags == 0)
        {
            return false;
        }
        const auto polygon_type = static_cast<std::uint8_t>(prefix.area_and_type >> 6);
        const bool valid_vertex_count =
            (polygon_type == DetourPolyTypeGround && prefix.vertex_count >= 3 && prefix.vertex_count <= 6) ||
            (polygon_type == DetourPolyTypeOffMeshPoint && prefix.vertex_count == 2) ||
            (polygon_type == DetourPolyTypeOffMeshSegment && prefix.vertex_count >= 3 && prefix.vertex_count <= 6);
        if (!valid_vertex_count)
        {
            return false;
        }
        for (std::uint8_t index = 0; index < prefix.vertex_count; ++index)
        {
            if (prefix.vertices[index] >= static_cast<std::uint16_t>(vert_count))
            {
                return false;
            }
        }
        if (prefix.first_link == 0xffffffff || prefix.first_link < static_cast<std::uint32_t>(max_link_count))
        {
            return true;
        }
        return dynamic_links.structurally_valid && dynamic_links.data != nullptr &&
            prefix.first_link - static_cast<std::uint32_t>(max_link_count) <
                static_cast<std::uint32_t>(dynamic_links.size);
    }

    struct DecodedDynamicLinkReference
    {
        bool valid{false};
        std::uint32_t salt{};
        std::uint32_t tile_index{};
        std::uint32_t poly_index{};
    };

    DecodedDynamicLinkReference DecodeDynamicLinkReference(
        std::uint64_t reference, std::uint32_t tile_bits, std::uint32_t poly_bits)
    {
        DecodedDynamicLinkReference decoded;
        if (reference == 0 || tile_bits == 0 || poly_bits == 0 ||
            tile_bits + poly_bits >= 64 || tile_bits > 14 || poly_bits > 31)
        {
            return decoded;
        }
        const auto poly_mask = (std::uint64_t{1} << poly_bits) - 1;
        const auto tile_mask = (std::uint64_t{1} << tile_bits) - 1;
        const auto salt = reference >> (tile_bits + poly_bits);
        const auto tile_index = (reference >> poly_bits) & tile_mask;
        const auto poly_index = reference & poly_mask;
        if (salt == 0)
        {
            return decoded;
        }
        decoded.valid = true;
        decoded.salt = static_cast<std::uint32_t>(salt);
        decoded.tile_index = static_cast<std::uint32_t>(tile_index);
        decoded.poly_index = static_cast<std::uint32_t>(poly_index);
        return decoded;
    }

    enum class DynamicLinkPlausibilityReason
    {
        valid,
        valid_shipping_dynamic_edge_zero,
        valid_shipping_dynamic_edge_one,
        reference_zero_or_invalid,
        next_out_of_range,
        edge_not_shipping_dynamic,
        shipping_flag_missing,
        shipping_edge_zero_side_invalid,
        shipping_dynamic_geometry_nonzero,
        other,
    };

    const char* DynamicLinkPlausibilityReasonName(DynamicLinkPlausibilityReason reason)
    {
        switch (reason)
        {
        case DynamicLinkPlausibilityReason::valid:
            return "valid";
        case DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_zero:
            return "valid_shipping_dynamic_edge_zero";
        case DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_one:
            return "valid_shipping_dynamic_edge_one";
        case DynamicLinkPlausibilityReason::reference_zero_or_invalid:
            return "reference_zero_or_invalid";
        case DynamicLinkPlausibilityReason::next_out_of_range:
            return "next_out_of_range";
        case DynamicLinkPlausibilityReason::edge_not_shipping_dynamic:
            return "edge_not_shipping_dynamic";
        case DynamicLinkPlausibilityReason::shipping_flag_missing:
            return "shipping_flag_missing";
        case DynamicLinkPlausibilityReason::shipping_edge_zero_side_invalid:
            return "shipping_edge_zero_side_invalid";
        case DynamicLinkPlausibilityReason::shipping_dynamic_geometry_nonzero:
            return "shipping_dynamic_geometry_nonzero";
        case DynamicLinkPlausibilityReason::other:
            return "other";
        }
        return "other";
    }

    bool IsProvenShippingEdgeOneSide(std::uint8_t side)
    {
        return (side >= 0x40 && side <= 0x47) ||
            side == 0x60 || side == 0x61 || side == 0x62 ||
            side == 0x66 || side == 0x67 || side == 0xc0 ||
            side == 0xd0 || side == 0xe0;
    }

    DynamicLinkPlausibilityReason ClassifyDynamicLinkPlausibility(
        std::uint64_t reference, std::uint32_t next, std::uint8_t edge,
        std::uint8_t side, std::uint8_t bmin, std::uint8_t bmax,
        std::int32_t max_link_count, std::int32_t dynamic_size,
        std::uint32_t tile_bits, std::uint32_t poly_bits)
    {
        // dtNavMesh::getLink() treats firstLink/next as one global index
        // space: [0, maxLinkCount) addresses tile->links and values at or
        // above maxLinkCount address dynamicLinksO[index - maxLinkCount].
        const auto base_count = static_cast<std::uint32_t>(max_link_count);
        const auto dynamic_count = static_cast<std::uint32_t>(dynamic_size);
        const bool next_plausible = next == 0xffffffff || next < base_count ||
            (next >= base_count && next - base_count < dynamic_count);
        // The recovered shipping writers do not use one single stock Detour
        // portal encoding.  FUN_140a7803c writes live dynamicLinksO records
        // with edge=0 and side=0xd0/0xf0 (and clears bmin/bmax).  The related
        // FUN_140a7a2d8 path writes edge=1 records with the 0x40 shipping flag
        // and clears bmin/bmax.  Both records are inserted into real
        // firstLink/next chains.  Their route geometry remains unproven, so
        // acceptance here is structural only; graph code must not turn them
        // into ordinary portals.
        if (!DecodeDynamicLinkReference(reference, tile_bits, poly_bits).valid)
        {
            return DynamicLinkPlausibilityReason::reference_zero_or_invalid;
        }
        if (!next_plausible)
        {
            return DynamicLinkPlausibilityReason::next_out_of_range;
        }
        if (edge == 0x00)
        {
            if (side != 0xd0 && side != 0xf0)
            {
                return DynamicLinkPlausibilityReason::shipping_edge_zero_side_invalid;
            }
            if (bmin != 0 || bmax != 0)
            {
                return DynamicLinkPlausibilityReason::shipping_dynamic_geometry_nonzero;
            }
            return DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_zero;
        }
        if (edge == 0x01)
        {
            if (!IsProvenShippingEdgeOneSide(side))
            {
                return DynamicLinkPlausibilityReason::shipping_flag_missing;
            }
            if (bmin != 0 || bmax != 0)
            {
                return DynamicLinkPlausibilityReason::shipping_dynamic_geometry_nonzero;
            }
            return DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_one;
        }
        if (edge != 0xff)
        {
            return DynamicLinkPlausibilityReason::edge_not_shipping_dynamic;
        }
        const bool shipping_dynamic_flag_present = (side & 0x40) != 0;
        if (!shipping_dynamic_flag_present)
        {
            return DynamicLinkPlausibilityReason::shipping_flag_missing;
        }
        return DynamicLinkPlausibilityReason::valid;
    }

    bool IsAcceptedDynamicLinkPlausibilityReason(
        DynamicLinkPlausibilityReason reason)
    {
        return reason == DynamicLinkPlausibilityReason::valid ||
            reason == DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_zero ||
            reason == DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_one;
    }

    bool IsPlausibleDynamicLink(std::uint64_t reference, std::uint32_t next,
        std::uint8_t edge, std::uint8_t side, std::uint8_t bmin, std::uint8_t bmax,
        std::int32_t max_link_count,
        std::int32_t dynamic_size, std::uint32_t tile_bits = 14,
        std::uint32_t poly_bits = 31)
    {
        return IsAcceptedDynamicLinkPlausibilityReason(
            ClassifyDynamicLinkPlausibility(reference, next, edge, side,
                bmin, bmax, max_link_count, dynamic_size, tile_bits, poly_bits));
    }

    std::string Hex(const void* address)
    {
        std::ostringstream stream;
        stream << "0x" << std::hex << reinterpret_cast<std::uintptr_t>(address);
        return stream.str();
    }

    struct RawByteDump
    {
        bool readable{false};
        std::string hex;
    };

    RawByteDump ReadRawBytes(const void* address, std::size_t size)
    {
        RawByteDump dump;
        std::vector<std::uint8_t> bytes(size);
        if (!IsReadable(address, size) || (size > 0 && !GuardedCopy(bytes.data(), address, size)))
        {
            return dump;
        }
        static constexpr char Digits[] = "0123456789abcdef";
        dump.readable = true;
        dump.hex.reserve(size * 2);
        for (std::size_t index = 0; index < size; ++index)
        {
            dump.hex.push_back(Digits[(bytes[index] >> 4) & 0x0f]);
            dump.hex.push_back(Digits[bytes[index] & 0x0f]);
        }
        return dump;
    }

    std::string JsonRawByteDump(const RawByteDump& dump)
    {
        return std::string("{\"readable\":") + (dump.readable ? "true" : "false") +
            ",\"hex\":\"" + dump.hex + "\"}";
    }

    std::string JsonRawPolygonRecord(const void* polys, std::int32_t poly_index,
        std::uintptr_t record_stride = DtPolyStride)
    {
        const auto* address = static_cast<const std::uint8_t*>(polys) +
            static_cast<std::uintptr_t>(poly_index) * record_stride;
        std::uint32_t first_link = 0xffffffff;
        std::array<std::uint16_t, 6> vertices{};
        std::array<std::uint16_t, 6> neighbors{};
        std::uint16_t flags = 0;
        std::uint8_t vertex_count = 0;
        std::uint8_t area_and_type = 0;
        const bool fields_read =
            ReadValue(address, 0x00, first_link) &&
            ReadValue(address, 0x04, vertices) &&
            ReadValue(address, 0x10, neighbors) &&
            ReadValue(address, 0x1C, flags) &&
            ReadValue(address, 0x1E, vertex_count) &&
            ReadValue(address, 0x1F, area_and_type);
        std::ostringstream output;
        output << "{\"index\":" << poly_index
               << ",\"address\":\"" << Hex(address) << "\""
               << ",\"record_stride\":" << record_stride
               << ",\"raw_0x20\":" << JsonRawByteDump(ReadRawBytes(address, DtPolyStride))
               << ",\"fields_read\":" << (fields_read ? "true" : "false")
               << ",\"first_link\":" << first_link
               << ",\"vertex_count\":" << static_cast<unsigned int>(vertex_count)
               << ",\"area_and_type\":" << static_cast<unsigned int>(area_and_type)
               << ",\"flags\":" << flags << "}";
        return output.str();
    }

    bool ComputeSha256(const std::wstring& path, std::array<unsigned char, 32>& digest)
    {
        BCRYPT_ALG_HANDLE algorithm = nullptr;
        BCRYPT_HASH_HANDLE hash = nullptr;
        DWORD object_length = 0;
        DWORD hash_length = 0;
        DWORD result_length = 0;
        std::vector<unsigned char> object;
        bool success = false;

        if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0 ||
            BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                reinterpret_cast<PUCHAR>(&object_length), sizeof(object_length), &result_length, 0) != 0 ||
            BCryptGetProperty(algorithm, BCRYPT_HASH_LENGTH,
                reinterpret_cast<PUCHAR>(&hash_length), sizeof(hash_length), &result_length, 0) != 0 ||
            hash_length != digest.size())
        {
            if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
            return false;
        }

        object.resize(object_length);
        if (BCryptCreateHash(algorithm, &hash, object.data(), object_length, nullptr, 0, 0) != 0)
        {
            BCryptCloseAlgorithmProvider(algorithm, 0);
            return false;
        }

        std::ifstream input(path, std::ios::binary);
        std::array<unsigned char, 64 * 1024> buffer{};
        while (input)
        {
            input.read(reinterpret_cast<char*>(buffer.data()), static_cast<std::streamsize>(buffer.size()));
            const auto count = input.gcount();
            if (count > 0 && BCryptHashData(hash, buffer.data(), static_cast<ULONG>(count), 0) != 0)
            {
                BCryptDestroyHash(hash);
                BCryptCloseAlgorithmProvider(algorithm, 0);
                return false;
            }
        }

        success = input.eof() && BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) == 0;
        BCryptDestroyHash(hash);
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return success;
    }

    std::string BytesToUpperHex(const std::array<unsigned char, 32>& bytes)
    {
        constexpr char digits[] = "0123456789ABCDEF";
        std::string result;
        result.reserve(bytes.size() * 2);
        for (const auto byte : bytes)
        {
            result.push_back(digits[(byte >> 4) & 0x0F]);
            result.push_back(digits[byte & 0x0F]);
        }
        return result;
    }

    ExecutableIdentity GetExecutableIdentity()
    {
        ExecutableIdentity identity;
        std::array<wchar_t, MAX_PATH> buffer{};
        const auto length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (length == 0 || length >= buffer.size())
        {
            return identity;
        }
        identity.path.assign(buffer.data(), length);
        const auto separator = identity.path.find_last_of(L"\\/");
        std::wstring filename = separator == std::wstring::npos
            ? identity.path
            : identity.path.substr(separator + 1);
        for (auto& character : filename)
        {
            character = static_cast<wchar_t>(std::towlower(character));
        }
        identity.filename_matches = filename == L"ff7rebirth_.exe";

        std::array<unsigned char, 32> digest{};
        if (ComputeSha256(identity.path, digest))
        {
            identity.sha256 = BytesToUpperHex(digest);
            identity.sha256_matches = identity.sha256 == KnownGameSha256;
        }
        return identity;
    }

    std::string ReadTextFile(const wchar_t* path)
    {
        std::ifstream input(path, std::ios::binary);
        if (!input)
        {
            return {};
        }
        return std::string((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    }

    std::string ExtractJsonString(const std::string& json, const char* name)
    {
        const std::regex pattern(
            std::string("\\\"") + name + R"REGEX(\"\s*:\s*\"([^\"]*)\")REGEX");
        std::smatch match;
        if (!std::regex_search(json, match, pattern) || match.size() != 2)
        {
            return {};
        }
        return match[1].str();
    }

    std::string UtcNow()
    {
        const auto now = std::chrono::system_clock::now();
        const auto seconds = std::chrono::system_clock::to_time_t(now);
        std::tm utc{};
        gmtime_s(&utc, &seconds);
        const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
            now.time_since_epoch()).count() % 1000;
        std::ostringstream output;
        output << std::put_time(&utc, "%Y-%m-%dT%H:%M:%S")
               << '.' << std::setw(3) << std::setfill('0') << millis << 'Z';
        return output.str();
    }

    bool ReadCaptureRequest(CaptureRequest& request)
    {
        const auto json = ReadTextFile(GeometryEnablePath);
        if (json.empty())
        {
            return false;
        }
        request.request_id = ExtractJsonString(json, "request_id");
        request.requested_utc = ExtractJsonString(json, "requested_utc");
        request.session_id = ExtractJsonString(json, "session_id");
        request.world_id = ExtractJsonString(json, "world_id");
        request.map_id = ExtractJsonString(json, "map_id");
        request.executable_sha256 = ExtractJsonString(json, "executable_sha256");
        request.native_probe_sha256 = ExtractJsonString(json, "native_probe_sha256");
        request.purpose = ExtractJsonString(json, "purpose");
        request.valid = request.request_id.size() >= 16
            && request.requested_utc.size() >= 10
            && !request.session_id.empty()
            && !request.world_id.empty()
            && !request.map_id.empty()
            && request.purpose == "static_topology_cache_seed";
        return request.valid;
    }

    std::string Escape(const std::string& value)
    {
        std::string result;
        result.reserve(value.size());
        for (const char character : value)
        {
            if (character == '\\') result += "\\\\";
            else if (character == '"') result += "\\\"";
            else if (character == '\n') result += "\\n";
            else if (static_cast<unsigned char>(character) >= 0x20) result += character;
        }
        return result;
    }

    bool ConsumeOneShotMarker(const wchar_t* path)
    {
        SetLastError(ERROR_SUCCESS);
        return DeleteFileW(path) != 0 || GetLastError() == ERROR_FILE_NOT_FOUND;
    }

    std::string Escape(const std::wstring& value)
    {
        std::string result;
        result.reserve(value.size());
        for (const wchar_t character : value)
        {
            if (character == L'\\') result += "\\\\";
            else if (character == L'\"') result += "\\\"";
            else if (character == L'\n') result += "\\n";
            else if (character >= 0 && character <= 0x7F) result += static_cast<char>(character);
            else result += '?';
        }
        return result;
    }

    bool ReadTarget(Target& target)
    {
        std::ifstream input(TargetPath);
        if (!input)
        {
            return false;
        }
        const std::string json((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
        std::smatch match;
        const std::regex id_pattern(R"REGEX("target_id"\s*:\s*"([^"]+)")REGEX");
        const std::regex position_pattern(R"REGEX("position"\s*:\s*\{\s*"x"\s*:\s*(-?[0-9.eE+]+)\s*,\s*"y"\s*:\s*(-?[0-9.eE+]+)\s*,\s*"z"\s*:\s*(-?[0-9.eE+]+))REGEX");
        if (!std::regex_search(json, match, id_pattern) || match.size() != 2)
        {
            return false;
        }
        target.id = match[1].str();
        if (!std::regex_search(json, match, position_pattern) || match.size() != 4)
        {
            return false;
        }
        try
        {
            target.position.x = std::stof(match[1].str());
            target.position.y = std::stof(match[2].str());
            target.position.z = std::stof(match[3].str());
        }
        catch (...)
        {
            return false;
        }
        const std::regex start_position_pattern(R"REGEX("start_position"\s*:\s*\{\s*"x"\s*:\s*(-?[0-9.eE+]+)\s*,\s*"y"\s*:\s*(-?[0-9.eE+]+)\s*,\s*"z"\s*:\s*(-?[0-9.eE+]+))REGEX");
        if (std::regex_search(json, match, start_position_pattern) && match.size() == 4)
        {
            try
            {
                target.start_position.x = std::stof(match[1].str());
                target.start_position.y = std::stof(match[2].str());
                target.start_position.z = std::stof(match[3].str());
                target.has_start_position = std::isfinite(target.start_position.x) &&
                    std::isfinite(target.start_position.y) && std::isfinite(target.start_position.z);
            }
            catch (...)
            {
                target.has_start_position = false;
            }
        }
        return std::isfinite(target.position.x) && std::isfinite(target.position.y) && std::isfinite(target.position.z);
    }

    bool ReadPlayerPosition(Vec3& position)
    {
        std::ifstream input(StatePath);
        if (!input)
        {
            return false;
        }
        const std::string json((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
        const auto player_offset = json.find("\"player\"");
        if (player_offset == std::string::npos)
        {
            return false;
        }
        const std::string player_json = json.substr(player_offset);
        std::smatch match;
        const std::regex position_pattern(R"REGEX("position"\s*:\s*\{\s*"x"\s*:\s*(-?[0-9.eE+]+)\s*,\s*"y"\s*:\s*(-?[0-9.eE+]+)\s*,\s*"z"\s*:\s*(-?[0-9.eE+]+))REGEX");
        if (!std::regex_search(player_json, match, position_pattern) || match.size() != 4)
        {
            return false;
        }
        try
        {
            position.x = std::stof(match[1].str());
            position.y = std::stof(match[2].str());
            position.z = std::stof(match[3].str());
        }
        catch (...)
        {
            return false;
        }
        return std::isfinite(position.x) && std::isfinite(position.y) && std::isfinite(position.z);
    }

    std::string JsonVec(const Vec3& value)
    {
        std::ostringstream stream;
        stream << "{\"x\":" << value.x << ",\"y\":" << value.y << ",\"z\":" << value.z << "}";
        return stream.str();
    }

    std::string JsonString(const std::wstring& value)
    {
        return "\"" + Escape(value) + "\"";
    }

    bool ReadRawTArray(const void* base, std::uintptr_t offset, RawTArray& array)
    {
        return ReadValue(base, offset + 0x00, array.data) &&
            ReadValue(base, offset + 0x08, array.count) &&
            ReadValue(base, offset + 0x0C, array.capacity) &&
            array.count >= 0 && array.capacity >= array.count;
    }

    struct SplineProbe
    {
        std::string status{"not_attempted"};
        std::string reason_code;
        std::int32_t point_count{-1};
        std::int32_t points_returned{0};
        bool point_list_complete{false};
        float length{0.0f};
        bool length_valid{false};
        std::vector<Vec3> points;
    };

    struct TileLookupSample
    {
        std::int32_t lookup_index{-1};
        void* tile{};
        std::uint32_t salt{};
        void* header{};
        std::int32_t version{};
        std::int32_t x{};
        std::int32_t y{};
        std::int32_t layer{};
        std::int32_t poly_count{};
        std::int32_t vert_count{};
    };

    struct TileMemorySample
    {
        std::int32_t lookup_index{-1};
        void* tile{};
        std::array<std::uintptr_t, 14> words{};
        std::array<bool, 14> word_readable{};
        void* header_candidate{};
        bool header_readable{false};
        std::array<std::uint32_t, 16> header_words{};
        bool header_minus_four_readable{false};
        std::array<std::uint32_t, 16> header_minus_four_words{};
    };

    struct TileLookupEntry
    {
        TileLookupSample sample;
        std::int32_t chain_index{};
    };

    struct TileLookupInventory
    {
        std::int32_t lookup_size{};
        std::int32_t non_empty_buckets{};
        std::int32_t pointer_entries_non_null{};
        std::int32_t bucket_heads{};
        std::int32_t valid_tiles{};
        std::int32_t unique_active_tiles{};
        std::int32_t chained_tiles{};
        std::int32_t longest_chain{};
        std::int32_t nodes_examined{};
        std::int32_t rejected_pointers{};
        std::int32_t rejected_tile_headers{};
        std::int32_t cyclic_pointers{};
        std::int32_t duplicate_pointers{};
        std::int32_t chain_bound_hits{};
        std::int32_t global_bound_hits{};
        bool full_lookup_chain_traversed{false};
        std::vector<TileLookupSample> samples;
        std::vector<TileMemorySample> rejected_samples;
        std::vector<TileLookupEntry> tiles;
    };

    TileMemorySample CaptureTileMemorySample(void* tile, std::int32_t lookup_index)
    {
        TileMemorySample sample;
        sample.lookup_index = lookup_index;
        sample.tile = tile;
        for (std::size_t index = 0; index < sample.words.size(); ++index)
        {
            const auto offset = static_cast<std::uintptr_t>(index * sizeof(void*));
            sample.word_readable[index] = IsReadable(tile, offset + sizeof(void*));
            if (sample.word_readable[index])
            {
                ReadValue(tile, offset, sample.words[index]);
            }
        }
        sample.header_candidate = reinterpret_cast<void*>(sample.words[1]);
        sample.header_readable = IsReadable(sample.header_candidate, sizeof(sample.header_words));
        if (sample.header_readable)
        {
            for (std::size_t index = 0; index < sample.header_words.size(); ++index)
            {
                ReadValue(sample.header_candidate, index * sizeof(std::uint32_t), sample.header_words[index]);
            }
        }
        if (sample.header_candidate != nullptr && IsReadable(sample.header_candidate))
        {
            auto* preceding = static_cast<std::uint8_t*>(sample.header_candidate) - sizeof(std::uint32_t);
            sample.header_minus_four_readable = IsReadable(preceding, sizeof(sample.header_minus_four_words));
            if (sample.header_minus_four_readable)
            {
                for (std::size_t index = 0; index < sample.header_minus_four_words.size(); ++index)
                {
                    ReadValue(preceding, index * sizeof(std::uint32_t), sample.header_minus_four_words[index]);
                }
            }
        }
        return sample;
    }

    bool ReadValidTileHeader(void* tile, std::int32_t lookup_index, TileLookupSample& sample)
    {
        if (tile == nullptr || !IsReadable(tile, 0x10))
        {
            return false;
        }
        sample = TileLookupSample{};
        sample.lookup_index = lookup_index;
        sample.tile = tile;
        if (!ReadValue(tile, 0x00, sample.salt))
        {
            return false;
        }
        std::uint32_t magic = 0;
        if (!ReadValue(tile, 0x08, sample.header) || !IsReadable(sample.header, 0x20) ||
            !ReadValue(sample.header, 0x00, magic) || magic != DetourNavMeshMagic ||
            !ReadValue(sample.header, 0x04, sample.version) ||
            (sample.version != DetourNavMeshVersion &&
             sample.version != RebirthDetourNavMeshVersion) ||
            !ReadValue(sample.header, 0x08, sample.x) ||
            !ReadValue(sample.header, 0x0C, sample.y) ||
            !ReadValue(sample.header, 0x10, sample.layer) ||
            !ReadValue(sample.header, 0x18, sample.poly_count) ||
            !ReadValue(sample.header, 0x1C, sample.vert_count) ||
            sample.poly_count <= 0 || sample.poly_count > 1000000 ||
            sample.vert_count <= 0 || sample.vert_count > 1000000)
        {
            return false;
        }
        return true;
    }

    TileLookupInventory TraverseTileLookup(void* lookup, std::int32_t lookup_size)
    {
        TileLookupInventory inventory;
        inventory.lookup_size = lookup_size;
        if (lookup == nullptr || !IsReadable(lookup) || lookup_size <= 0 || lookup_size > 65536)
        {
            return inventory;
        }
        inventory.samples.reserve(32);
        inventory.tiles.reserve(4096);
        std::unordered_set<std::uintptr_t> visited_tiles;
        visited_tiles.reserve(4096);
        inventory.full_lookup_chain_traversed = true;
        for (std::int32_t index = 0; index < lookup_size; ++index)
        {
            void* tile = nullptr;
            if (!ReadValue(static_cast<const std::uint8_t*>(lookup) +
                               static_cast<std::uintptr_t>(index) * sizeof(void*),
                           0,
                           tile))
            {
                inventory.full_lookup_chain_traversed = false;
                continue;
            }
            if (tile == nullptr)
            {
                continue;
            }
            ++inventory.non_empty_buckets;
            ++inventory.pointer_entries_non_null;
            ++inventory.bucket_heads;

            std::unordered_set<std::uintptr_t> chain_seen;
            chain_seen.reserve(64);
            std::int32_t chain_length = 0;
            while (tile != nullptr)
            {
                const auto tile_address = reinterpret_cast<std::uintptr_t>(tile);
                if (chain_length >= MaxLookupChainLength)
                {
                    ++inventory.chain_bound_hits;
                    inventory.full_lookup_chain_traversed = false;
                    break;
                }
                if (!chain_seen.insert(tile_address).second)
                {
                    ++inventory.cyclic_pointers;
                    inventory.full_lookup_chain_traversed = false;
                    break;
                }
                if (inventory.nodes_examined >= MaxLookupTilesVisited)
                {
                    ++inventory.global_bound_hits;
                    inventory.full_lookup_chain_traversed = false;
                    break;
                }
                ++inventory.nodes_examined;
                ++chain_length;

                if (visited_tiles.find(tile_address) != visited_tiles.end())
                {
                    ++inventory.duplicate_pointers;
                    inventory.full_lookup_chain_traversed = false;
                    break;
                }
                visited_tiles.insert(tile_address);

                TileLookupSample sample;
                if (!ReadValidTileHeader(tile, index, sample))
                {
                    ++inventory.rejected_pointers;
                    ++inventory.rejected_tile_headers;
                    inventory.full_lookup_chain_traversed = false;
                    if (inventory.rejected_samples.size() < 16)
                    {
                        inventory.rejected_samples.push_back(CaptureTileMemorySample(tile, index));
                    }
                }
                else
                {
                    ++inventory.valid_tiles;
                    ++inventory.unique_active_tiles;
                    if (chain_length > 1)
                    {
                        ++inventory.chained_tiles;
                    }
                    inventory.longest_chain = (std::max)(inventory.longest_chain, chain_length);
                    if (inventory.samples.size() < 32)
                    {
                        inventory.samples.push_back(sample);
                    }
                    inventory.tiles.push_back(TileLookupEntry{sample, chain_length - 1});
                }

                void* next_tile = nullptr;
                if (!ReadValue(tile, DtMeshTileNextOffset, next_tile))
                {
                    ++inventory.rejected_pointers;
                    inventory.full_lookup_chain_traversed = false;
                    break;
                }
                if (next_tile != nullptr && chain_length >= MaxLookupChainLength)
                {
                    ++inventory.chain_bound_hits;
                    inventory.full_lookup_chain_traversed = false;
                    break;
                }
                tile = next_tile;
            }
            inventory.longest_chain = (std::max)(inventory.longest_chain, chain_length);
        }
        return inventory;
    }

    TileLookupInventory ScanTileLookup(void* lookup, std::int32_t lookup_size)
    {
        return TraverseTileLookup(lookup, lookup_size);
    }

    Vec3 RecastToUnreal(const Vec3& value)
    {
        return Vec3{-value.x, -value.z, value.y};
    }

    struct TopologyTile
    {
        TileLookupSample sample;
        std::uint32_t tile_index{};
        void* polys{};
        void* verts{};
        void* links{};
        std::int32_t max_link_count{};
        ChunkArrayObservation dynamic_links_o;
    };

    struct TopologyPolygon
    {
        std::uint32_t tile_index{};
        std::uint32_t salt{};
        std::int32_t poly_index{};
        std::uint64_t reference{};
        std::uint32_t first_link{0xffffffff};
        Vec3 centroid_world{};
        std::vector<Vec3> vertices_world;
        std::vector<std::size_t> neighbours;
        std::vector<std::uint64_t> neighbour_refs;
    };

    std::string BuildTopologyProof(const TileLookupInventory& inventory,
        void* detour_tiles, std::int32_t detour_max_tiles,
        std::uint32_t tile_bits, std::uint32_t poly_bits,
        const Vec3* player_position)
    {
        std::ostringstream output;
        output << "{\"reason_code\":";
        if (player_position == nullptr)
        {
            output << "\"player_position_unavailable\",\"status\":\"not_proven\"}";
            return output.str();
        }
        if (detour_tiles == nullptr || detour_max_tiles <= 0 ||
            detour_max_tiles > 20000 || tile_bits == 0 || poly_bits == 0 ||
            tile_bits + poly_bits >= 64)
        {
            output << "\"reference_shift_layout_unavailable\",\"status\":\"not_proven\"}";
            return output.str();
        }

        const std::uint64_t poly_mask = (std::uint64_t{1} << poly_bits) - 1;
        const std::uint64_t tile_mask = (std::uint64_t{1} << tile_bits) - 1;
        auto tile_index_for_pointer = [&](void* tile, std::uint32_t& tile_index) {
            const auto base = reinterpret_cast<std::uintptr_t>(detour_tiles);
            const auto address = reinterpret_cast<std::uintptr_t>(tile);
            if (base == 0 || address < base ||
                (address - base) % DtMeshTileStride != 0)
            {
                return false;
            }
            const auto index = (address - base) / DtMeshTileStride;
            if (index >= static_cast<std::uintptr_t>(detour_max_tiles))
            {
                return false;
            }
            tile_index = static_cast<std::uint32_t>(index);
            return true;
        };
        auto encode_reference = [&](std::uint32_t salt, std::uint32_t tile_index,
            std::uint32_t poly_index) {
            return (static_cast<std::uint64_t>(salt) << (tile_bits + poly_bits)) |
                (static_cast<std::uint64_t>(tile_index) << poly_bits) |
                static_cast<std::uint64_t>(poly_index);
        };

        // Lookup buckets are not a tile limit: chained tiles pushed the
        // Under Junon inventory to 4,653. Bound both passes by the full scan.
        constexpr std::size_t MaxTopologyTiles = MaxLookupTilesVisited;
        constexpr std::size_t MaxTopologyPolygons = 250000;
        std::vector<TopologyTile> tiles;
        tiles.reserve((std::min)(inventory.tiles.size(), MaxTopologyTiles));
        std::unordered_map<std::uint32_t, std::size_t> tile_by_index;
        std::size_t tile_rejections = 0;
        for (const auto& entry : inventory.tiles)
        {
            if (tiles.size() >= MaxTopologyTiles)
            {
                break;
            }
            TopologyTile tile;
            tile.sample = entry.sample;
            if (!tile_index_for_pointer(tile.sample.tile, tile.tile_index) ||
                !ReadValue(tile.sample.tile, 0x10, tile.polys) ||
                !ReadValue(tile.sample.tile, 0x18, tile.verts) ||
                !ReadValue(tile.sample.tile, 0x20, tile.links) ||
                !ReadValue(tile.sample.header, 0x20, tile.max_link_count) ||
                tile.max_link_count < 0 || tile.max_link_count > 1000000 ||
                tile.polys == nullptr || tile.verts == nullptr ||
                (tile.max_link_count > 0 && tile.links == nullptr))
            {
                ++tile_rejections;
                continue;
            }
            tile.dynamic_links_o = ReadChunkArray(tile.sample.tile,
                DtMeshTileDynamicLinksOOffset);
            if (!tile.dynamic_links_o.fields_read ||
                !tile.dynamic_links_o.structurally_valid ||
                (tile.dynamic_links_o.size > 0 && tile.dynamic_links_o.data == nullptr))
            {
                ++tile_rejections;
                continue;
            }
            const auto insert_result = tile_by_index.emplace(tile.tile_index, tiles.size());
            if (!insert_result.second)
            {
                continue;
            }
            tiles.push_back(tile);
        }

        std::vector<TopologyPolygon> polygons;
        polygons.reserve((std::min)(MaxTopologyPolygons, inventory.unique_active_tiles * 8ull));
        std::unordered_map<std::uint64_t, std::size_t> polygon_by_reference;
        std::size_t polygon_rejections = 0;
        for (const auto& tile : tiles)
        {
            for (std::int32_t poly_index = 0; poly_index < tile.sample.poly_count; ++poly_index)
            {
                if (polygons.size() >= MaxTopologyPolygons)
                {
                    break;
                }
                const auto prefix = ReadDtPolyPrefix(tile.polys, poly_index,
                    ShippingPolyRecordStride);
                if (!IsValidDtPolyPrefix(prefix, tile.sample.vert_count,
                    tile.max_link_count, tile.dynamic_links_o))
                {
                    ++polygon_rejections;
                    continue;
                }
                TopologyPolygon polygon;
                polygon.tile_index = tile.tile_index;
                polygon.salt = tile.sample.salt;
                polygon.poly_index = poly_index;
                polygon.reference = encode_reference(tile.sample.salt, tile.tile_index,
                    static_cast<std::uint32_t>(poly_index));
                polygon.first_link = prefix.first_link;
                polygon.vertices_world.reserve(prefix.vertex_count);
                Vec3 centroid{};
                bool vertices_valid = true;
                for (std::uint8_t vertex_index = 0; vertex_index < prefix.vertex_count; ++vertex_index)
                {
                    Vec3 recast_vertex{};
                    const auto* vertex = static_cast<const std::uint8_t*>(tile.verts) +
                        static_cast<std::uintptr_t>(prefix.vertices[vertex_index]) * DtRecastVertexStride;
                    if (!ReadValue(vertex, 0, recast_vertex) ||
                        !std::isfinite(recast_vertex.x) || !std::isfinite(recast_vertex.y) ||
                        !std::isfinite(recast_vertex.z))
                    {
                        vertices_valid = false;
                        break;
                    }
                    const auto world_vertex = RecastToUnreal(recast_vertex);
                    polygon.vertices_world.push_back(world_vertex);
                    centroid.x += world_vertex.x;
                    centroid.y += world_vertex.y;
                    centroid.z += world_vertex.z;
                }
                if (!vertices_valid || polygon.vertices_world.empty())
                {
                    ++polygon_rejections;
                    continue;
                }
                const auto divisor = static_cast<float>(polygon.vertices_world.size());
                polygon.centroid_world = Vec3{centroid.x / divisor, centroid.y / divisor,
                    centroid.z / divisor};
                polygon_by_reference.emplace(polygon.reference, polygons.size());
                polygons.push_back(std::move(polygon));
            }
        }

        std::size_t same_tile_links = 0;
        std::size_t cross_tile_links = 0;
        std::size_t link_read_failures = 0;
        for (std::size_t polygon_index = 0; polygon_index < polygons.size(); ++polygon_index)
        {
            auto& polygon = polygons[polygon_index];
            const auto tile_it = tile_by_index.find(polygon.tile_index);
            if (tile_it == tile_by_index.end())
            {
                continue;
            }
            const auto& tile = tiles[tile_it->second];
            std::uint32_t link_index = polygon.first_link;
            for (std::int32_t hops = 0; link_index != 0xffffffff && hops < 4096; ++hops)
            {
                const bool dynamic = link_index >= static_cast<std::uint32_t>(tile.max_link_count);
                const std::uint8_t* link = nullptr;
                if (!dynamic)
                {
                    link = static_cast<const std::uint8_t*>(tile.links) +
                        static_cast<std::uintptr_t>(link_index) * DtLinkStride;
                }
                else
                {
                    const auto dynamic_index = link_index -
                        static_cast<std::uint32_t>(tile.max_link_count);
                    if (dynamic_index >= static_cast<std::uint32_t>(tile.dynamic_links_o.size))
                    {
                        break;
                    }
                    link = static_cast<const std::uint8_t*>(tile.dynamic_links_o.data) +
                        static_cast<std::uintptr_t>(dynamic_index) * DtLinkStride;
                }
                std::uint64_t reference = 0;
                std::uint32_t next = 0xffffffff;
                if (!ReadValue(link, 0x00, reference) || !ReadValue(link, 0x08, next))
                {
                    ++link_read_failures;
                    break;
                }
                if (reference != 0)
                {
                    const auto target_it = polygon_by_reference.find(reference);
                    if (target_it != polygon_by_reference.end())
                    {
                        polygon.neighbour_refs.push_back(reference);
                        polygon.neighbours.push_back(target_it->second);
                        if (polygons[target_it->second].tile_index == polygon.tile_index)
                        {
                            ++same_tile_links;
                        }
                        else
                        {
                            ++cross_tile_links;
                        }
                    }
                }
                link_index = next;
            }
        }

        std::size_t nearby_index = polygons.size();
        float nearby_distance = (std::numeric_limits<float>::max)();
        for (std::size_t index = 0; index < polygons.size(); ++index)
        {
            const auto& polygon = polygons[index];
            const auto dx = polygon.centroid_world.x - player_position->x;
            const auto dy = polygon.centroid_world.y - player_position->y;
            const auto dz = polygon.centroid_world.z - player_position->z;
            const auto distance = dx * dx + dy * dy + dz * dz;
            if (distance < nearby_distance)
            {
                nearby_distance = distance;
                nearby_index = index;
            }
        }
        std::size_t same_index = polygons.size();
        std::size_t cross_index = polygons.size();
        if (nearby_index < polygons.size())
        {
            for (const auto same_candidate : polygons[nearby_index].neighbours)
            {
                if (polygons[same_candidate].tile_index == polygons[nearby_index].tile_index)
                {
                    same_index = same_candidate;
                    break;
                }
            }
            if (same_index < polygons.size())
            {
                for (const auto cross_candidate : polygons[same_index].neighbours)
                {
                    if (polygons[cross_candidate].tile_index != polygons[same_index].tile_index)
                    {
                        cross_index = cross_candidate;
                        break;
                    }
                }
            }
        }

        auto append_polygon = [&](const char* name, std::size_t index) {
            output << (name == std::string("nearby") ? "\"nearby_polygon\":" :
                       name == std::string("same") ? "\"same_tile_adjacent\":" :
                       "\"cross_tile_adjacent\":");
            if (index >= polygons.size())
            {
                output << "null";
                return;
            }
            const auto& polygon = polygons[index];
            output << "{\"tile_index\":" << polygon.tile_index
                   << ",\"poly_index\":" << polygon.poly_index
                   << ",\"salt\":" << polygon.salt
                   << ",\"reference\":" << polygon.reference
                   << ",\"centroid_world\":" << JsonVec(polygon.centroid_world)
                   << ",\"vertices_world\":[";
            for (std::size_t vertex_index = 0; vertex_index < polygon.vertices_world.size(); ++vertex_index)
            {
                if (vertex_index != 0) output << ",";
                output << JsonVec(polygon.vertices_world[vertex_index]);
            }
            output << "]}";
        };

        const bool proved = nearby_index < polygons.size() && same_index < polygons.size() &&
            cross_index < polygons.size() &&
            polygons[nearby_index].tile_index == polygons[same_index].tile_index &&
            polygons[same_index].tile_index != polygons[cross_index].tile_index;
        output << (proved ? "\"cross_tile_topology_proved\"" :
            "\"no_nearby_two_hop_cross_tile_link\"")
               << ",\"tile_bits\":" << tile_bits
               << ",\"poly_bits\":" << poly_bits
               << ",\"tiles_considered\":" << tiles.size()
               << ",\"tile_rejections\":" << tile_rejections
               << ",\"polygons_valid\":" << polygons.size()
               << ",\"polygon_rejections\":" << polygon_rejections
               << ",\"same_tile_links\":" << same_tile_links
               << ",\"cross_tile_links\":" << cross_tile_links
               << ",\"link_read_failures\":" << link_read_failures
               << ",\"player_position\":" << JsonVec(*player_position)
               << ",\"nearby_distance_squared\":"
               << (nearby_index < polygons.size() ? nearby_distance : -1.0f) << ",";
        append_polygon("nearby", nearby_index);
        output << ",";
        append_polygon("same", same_index);
        output << ",";
        append_polygon("cross", cross_index);
        output << ",\"movement_sent\":false,\"status\":\""
               << (proved ? "proved" : "not_proven") << "\"}";
        return output.str();
    }

    // Export bounded diagnostic geometry only under the separate one-shot
    // geometry marker. This output is deliberately not written to the
    // production route sidecar or consumed by movement code. The executable
    // has now independently established the record/link layout, but this
    // remains a diagnostic exporter until a separate topology proof exists.
    std::string BuildTileGeometryDump(const TileLookupInventory& inventory,
        void* detour_tiles, std::int32_t detour_max_tiles,
        std::uint32_t detour_tile_bits, std::uint32_t detour_poly_bits,
        const Vec3* player_position, bool& geometry_export_complete)
    {
        geometry_export_complete = false;
        constexpr std::int32_t MaxExportTiles = MaxLookupTilesVisited;
        constexpr std::int32_t MaxTotalPolys = 500000;
        constexpr std::int32_t MaxTotalVertices = 1000000;
        constexpr std::int32_t MaxLinksPerPoly = 4096;
        constexpr std::int32_t MaxOffMeshConnectionsPerTile = 100000;
        std::ostringstream output;
        output << "{\"schema_version\":2,\"source\":\"ue4ss_native_detour_geometry_dump\","
               << "\"geometry_mode\":\"shipping_extended_poly_record_diagnostic\","
               << "\"source_poly_stride\":" << DtPolyStride
               << ",\"shipping_poly_record_stride\":" << ShippingPolyRecordStride
               << ",\"shipping_binary_layout_proven\":"
               << (ShippingBinaryLayoutProven ? "true" : "false")
               << ",\"dynamic_links_o_offset\":" << DtMeshTileDynamicLinksOOffset
               << ",\"dynamic_free_list_o_offset\":" << DtMeshTileDynamicFreeListOOffset
               << ",\"candidate_export_authorized\":false,"
               << "\"coordinate_space\":\"recast\","
               << "\"coordinate_transform_to_unreal\":\"x=-recast.x,y=-recast.z,z=recast.y\","
               << "\"movement_sent\":false,\"lookup_size\":" << inventory.lookup_size
               << ",\"pointer_entries_non_null\":" << inventory.pointer_entries_non_null
               << ",\"valid_tiles_seen\":" << inventory.valid_tiles
               << ",\"non_empty_buckets\":" << inventory.non_empty_buckets
               << ",\"unique_active_tiles\":" << inventory.unique_active_tiles
               << ",\"chained_tiles\":" << inventory.chained_tiles
               << ",\"longest_chain\":" << inventory.longest_chain
               << ",\"rejected_pointers\":" << inventory.rejected_pointers
               << ",\"cyclic_pointers\":" << inventory.cyclic_pointers
               << ",\"full_lookup_chain_traversed\":"
               << (inventory.full_lookup_chain_traversed ? "true" : "false")
               << ",\"tiles\":[";

        std::int32_t exported_tiles = 0;
        std::int32_t total_polys = 0;
        std::int32_t total_vertices = 0;
        bool truncated = false;
        bool first_tile = true;
        std::int32_t tiles_considered = 0;
        std::int32_t tiles_skipped_basic = 0;
        std::int32_t tiles_skipped_vertices = 0;
        std::int32_t tiles_skipped_polygons_or_links = 0;
        std::int32_t tiles_skipped_off_mesh = 0;
        std::int32_t polygon_read_failures = 0;
        std::int32_t polygon_first_link_read_failures = 0;
        std::int32_t polygon_vertex_array_read_failures = 0;
        std::int32_t polygon_neighbor_array_read_failures = 0;
        std::int32_t polygon_flags_read_failures = 0;
        std::int32_t polygon_vertex_count_read_failures = 0;
        std::int32_t polygon_area_type_read_failures = 0;
        std::int32_t polygon_vertex_count_invalid = 0;
        std::int32_t polygon_off_mesh_vertices_accepted = 0;
        std::int32_t polygon_vertex_index_failures = 0;
        std::int32_t link_pointer_rejections = 0;
        std::int32_t link_array_null_rejections = 0;
        std::int32_t link_index_out_of_range = 0;
        std::int32_t link_read_failures = 0;
        std::int32_t link_chain_bound_hits = 0;
        std::int32_t dynamic_link_array_read_failures = 0;
        std::int32_t dynamic_link_array_invalid_rejections = 0;
        std::int32_t dynamic_link_array_null_rejections = 0;
        std::int32_t dynamic_link_index_out_of_range = 0;
        std::int32_t dynamic_links_used = 0;
        std::int32_t off_mesh_pointer_rejections = 0;
        std::int32_t off_mesh_read_failures = 0;
        std::int32_t dynamic_link_arrays_structurally_valid = 0;
        std::int32_t dynamic_link_arrays_null_data = 0;
        std::int32_t dynamic_link_arrays_invalid_size_capacity = 0;
        std::int32_t dynamic_link_arrays_unreadable_fields = 0;
        std::int32_t dynamic_links_resolved = 0;
        std::int32_t dynamic_links_route_semantics_unresolved = 0;
        std::int32_t dynamic_links_valid_edge_zero = 0;
        std::int32_t dynamic_links_valid_edge_one = 0;
        std::int32_t candidate_a0_fields_read = 0;
        std::int32_t candidate_a0_structurally_valid = 0;
        std::int32_t candidate_a0_null_data = 0;
        std::int32_t candidate_a0_invalid_size_capacity = 0;
        std::int32_t candidate_a0_unreadable_fields = 0;
        std::int32_t candidate_a0_link_attempts = 0;
        std::int32_t candidate_a0_link_resolved = 0;
        std::int32_t candidate_a0_link_valid_edge_zero = 0;
        std::int32_t candidate_a0_link_valid_edge_one = 0;
        std::int32_t candidate_a0_link_data_rejections = 0;
        std::int32_t candidate_a0_link_array_rejections = 0;
        std::int32_t candidate_a0_link_index_rejections = 0;
        std::int32_t candidate_a0_link_read_failures = 0;
        std::int32_t candidate_a0_link_plausibility_failures = 0;
        std::int32_t candidate_a0_link_plausibility_failure_tiles = 0;
        std::int32_t candidate_a0_link_plausibility_failure_tiles_multiple = 0;
        std::int32_t candidate_a0_link_plausibility_failure_max_per_tile = 0;
        std::int32_t candidate_a0_tiles_with_success = 0;
        std::int32_t tiles_with_incomplete_link_data = 0;
        std::int32_t polygons_with_incomplete_link_data = 0;
        std::int32_t shipping_poly_block_tiles_sampled = 0;
        std::int32_t shipping_poly_block_prefixes_sampled = 0;
        std::int32_t shipping_poly_block_prefixes_valid = 0;
        std::int32_t shipping_poly_block_prefixes_link_ref_plausible = 0;
        std::int32_t shipping_poly_block_tiles_all_prefixes_valid = 0;
        std::int32_t shipping_poly_block_tiles_all_link_refs_plausible = 0;
        std::int32_t shipping_poly_block_links_pointer_matches = 0;
        std::int32_t shipping_poly_block_links_pointer_mismatches = 0;
        std::int32_t shipping_poly_block_payload_bounds_valid = 0;
        std::int32_t shipping_poly_block_payload_bounds_invalid = 0;
        std::vector<std::string> rejection_samples;
        rejection_samples.reserve(32);
        std::vector<std::string> memory_diagnostic_samples;
        memory_diagnostic_samples.reserve(MaxMemoryDiagnosticTiles);
        std::vector<std::string> shipping_poly_block_diagnostic_samples;
        shipping_poly_block_diagnostic_samples.reserve(MaxMemoryDiagnosticTiles);
        std::vector<std::string> candidate_a0_link_diagnostic_samples;
        candidate_a0_link_diagnostic_samples.reserve(MaxDynamicLinkPlausibilityDiagnosticSamples);
        std::map<std::string, std::int32_t> candidate_a0_link_rejection_reason_counts;
        std::map<unsigned int, std::int32_t> candidate_a0_link_rejection_edge_counts;
        std::map<unsigned int, std::int32_t> candidate_a0_link_rejection_side_counts;
        std::map<unsigned int, std::int32_t> candidate_a0_link_rejection_edge_side_counts;
        std::map<unsigned int, std::int32_t> candidate_a0_link_rejection_tile_failure_histogram;
        std::int32_t diagnostic_scan_tiles = 0;
        std::int32_t diagnostic_scan_incomplete_tiles = 0;
        std::int32_t diagnostic_scan_bound_hits = 0;
        std::int32_t diagnostic_scan_unreadable_links = 0;
        std::int32_t diagnostic_dynamic_records_scanned = 0;
        std::int32_t diagnostic_dynamic_link_plausibility_failures = 0;
        std::int32_t diagnostic_dynamic_link_plausibility_failure_tiles = 0;
        std::int32_t diagnostic_dynamic_link_plausibility_failure_tiles_multiple = 0;
        std::int32_t diagnostic_dynamic_link_plausibility_failure_tiles_multiple_reasons = 0;
        std::int32_t diagnostic_dynamic_link_plausibility_failure_max_per_tile = 0;
        std::int32_t diagnostic_dynamic_links_valid_edge_zero = 0;
        std::int32_t diagnostic_dynamic_links_valid_edge_one = 0;
        std::map<std::string, std::int32_t> diagnostic_rejection_reason_counts;
        std::map<unsigned int, std::int32_t> diagnostic_rejection_edge_counts;
        std::map<unsigned int, std::int32_t> diagnostic_rejection_side_counts;
        std::map<unsigned int, std::int32_t> diagnostic_rejection_edge_side_counts;
        std::map<unsigned int, std::int32_t> diagnostic_rejection_tile_failure_histogram;
        std::vector<std::string> diagnostic_dynamic_link_rejection_samples;
        diagnostic_dynamic_link_rejection_samples.reserve(
            MaxDynamicLinkPlausibilityDiagnosticSamples);
        std::map<std::string, std::int32_t> diagnostic_sample_counts_by_reason;
        auto tile_index_for_pointer = [&](void* tile, std::uint32_t& tile_index) {
            const auto base = reinterpret_cast<std::uintptr_t>(detour_tiles);
            const auto address = reinterpret_cast<std::uintptr_t>(tile);
            if (base == 0 || address < base ||
                (address - base) % DtMeshTileStride != 0)
            {
                return false;
            }
            const auto index = (address - base) / DtMeshTileStride;
            if (index >= static_cast<std::uintptr_t>(detour_max_tiles))
            {
                return false;
            }
            tile_index = static_cast<std::uint32_t>(index);
            return true;
        };
        auto append_string_count_object = [&output](
            const std::map<std::string, std::int32_t>& counts) {
            output << "{";
            bool first = true;
            for (const auto& entry : counts)
            {
                if (!first) output << ",";
                first = false;
                output << "\"" << entry.first << "\":" << entry.second;
            }
            output << "}";
        };
        auto append_numeric_count_object = [&output](
            const std::map<unsigned int, std::int32_t>& counts) {
            output << "{";
            bool first = true;
            for (const auto& entry : counts)
            {
                if (!first) output << ",";
                first = false;
                output << "\"" << entry.first << "\":" << entry.second;
            }
            output << "}";
        };
        auto append_edge_side_count_object = [&output](
            const std::map<unsigned int, std::int32_t>& counts) {
            output << "{";
            bool first = true;
            for (const auto& entry : counts)
            {
                if (!first) output << ",";
                first = false;
                const auto edge = (entry.first >> 8) & 0xff;
                const auto side = entry.first & 0xff;
                output << "\"edge_" << edge << "_side_" << side << "\":"
                       << entry.second;
            }
            output << "}";
        };
        auto record_diagnostic_failure = [&](const TileLookupSample& sample,
            void* tile, std::int32_t poly_index, std::uint32_t global_link_index,
            std::uint32_t dynamic_index, std::int32_t max_link_count,
            std::int32_t dynamic_size, std::int32_t dynamic_capacity,
            const std::uint8_t* link, std::uint64_t reference, std::uint32_t next,
            std::uint8_t edge, std::uint8_t side, std::uint8_t bmin,
            std::uint8_t bmax, DynamicLinkPlausibilityReason reason) {
            ++diagnostic_dynamic_link_plausibility_failures;
            const auto reason_name = DynamicLinkPlausibilityReasonName(reason);
            ++diagnostic_rejection_reason_counts[reason_name];
            ++diagnostic_rejection_edge_counts[static_cast<unsigned int>(edge)];
            ++diagnostic_rejection_side_counts[static_cast<unsigned int>(side)];
            ++diagnostic_rejection_edge_side_counts[
                (static_cast<unsigned int>(edge) << 8) |
                static_cast<unsigned int>(side)];
            if (diagnostic_sample_counts_by_reason[reason_name] >=
                    static_cast<std::int32_t>(MaxDiagnosticSamplesPerReason) ||
                diagnostic_dynamic_link_rejection_samples.size() >=
                    MaxDynamicLinkPlausibilityDiagnosticSamples)
            {
                return;
            }
            ++diagnostic_sample_counts_by_reason[reason_name];
            const auto decoded = DecodeDynamicLinkReference(
                reference, detour_tile_bits, detour_poly_bits);
            std::uint32_t source_tile_index = 0;
            const bool source_tile_index_valid =
                tile_index_for_pointer(tile, source_tile_index) && source_tile_index <= 0x3fff;
            const auto raw = ReadRawBytes(link, DtLinkStride);
            std::ostringstream diagnostic;
            diagnostic << "{\"kind\":\"dynamic_link_diagnostic\""
                       << ",\"reason\":\"" << reason_name << "\""
                       << ",\"lookup_index\":" << sample.lookup_index
                       << ",\"source_tile_index\":";
            if (source_tile_index_valid) diagnostic << source_tile_index;
            else diagnostic << "null";
            diagnostic << ",\"source_tile_index_valid\":"
                       << (source_tile_index_valid ? "true" : "false")
                       << ",\"source_salt\":" << sample.salt
                       << ",\"poly_index\":" << poly_index
                       << ",\"global_link_index\":" << global_link_index
                       << ",\"link_storage\":\"dynamic\""
                       << ",\"dynamic_index\":" << dynamic_index
                       << ",\"base_count\":" << max_link_count
                       << ",\"dynamic_count\":" << dynamic_size
                       << ",\"dynamic_capacity\":" << dynamic_capacity
                       << ",\"link_address\":\"" << Hex(link) << "\""
                       << ",\"reference\":" << reference
                       << ",\"decoded_target\":";
            if (!decoded.valid)
            {
                diagnostic << "null";
            }
            else
            {
                diagnostic << "{\"valid\":true,\"salt\":" << decoded.salt
                           << ",\"tile_index\":" << decoded.tile_index
                           << ",\"poly_index\":" << decoded.poly_index << "}";
            }
            diagnostic << ",\"next\":" << next
                       << ",\"edge\":" << static_cast<unsigned int>(edge)
                       << ",\"side\":" << static_cast<unsigned int>(side)
                       << ",\"bmin\":" << static_cast<unsigned int>(bmin)
                       << ",\"bmax\":" << static_cast<unsigned int>(bmax)
                       << ",\"raw_0x10\":" << JsonRawByteDump(raw)
                       << ",\"raw_16_hex\":\"" << raw.hex << "\"}";
            diagnostic_dynamic_link_rejection_samples.push_back(diagnostic.str());
        };
        auto scan_diagnostic_tile = [&](const TileLookupSample& sample, void* tile,
            void* polys, void* links, bool dynamic_links_array_valid,
            void* dynamic_links_data, std::int32_t dynamic_links_size,
            std::int32_t dynamic_links_capacity, std::int32_t max_link_count) {
            ++diagnostic_scan_tiles;
            bool scan_complete = true;
            std::size_t link_visits = 0;
            std::unordered_set<std::uint32_t> seen_dynamic_links;
            seen_dynamic_links.reserve(256);
            std::unordered_set<std::string> tile_reason_names;
            tile_reason_names.reserve(4);
            std::int32_t tile_failures = 0;
            const auto polygon_limit = (std::min)(
                sample.poly_count, static_cast<std::int32_t>(MaxDiagnosticPolygonsPerTile));
            if (sample.poly_count > static_cast<std::int32_t>(MaxDiagnosticPolygonsPerTile))
            {
                scan_complete = false;
                ++diagnostic_scan_bound_hits;
            }
            for (std::int32_t poly_index = 0; poly_index < polygon_limit && scan_complete;
                 ++poly_index)
            {
                const auto* poly = static_cast<const std::uint8_t*>(polys) +
                    static_cast<std::uintptr_t>(poly_index) * ShippingPolyRecordStride;
                std::uint32_t link_index = 0xffffffff;
                if (!ReadValue(poly, 0x00, link_index))
                {
                    scan_complete = false;
                    ++diagnostic_scan_unreadable_links;
                    break;
                }
                std::unordered_set<std::uint32_t> chain_seen;
                chain_seen.reserve(32);
                std::size_t chain_hops = 0;
                while (link_index != 0xffffffff)
                {
                    if (chain_hops++ >= MaxLinksPerPoly || link_visits++ >= MaxDiagnosticLinksPerTile)
                    {
                        scan_complete = false;
                        ++diagnostic_scan_bound_hits;
                        break;
                    }
                    if (!chain_seen.insert(link_index).second)
                    {
                        scan_complete = false;
                        ++diagnostic_scan_bound_hits;
                        break;
                    }
                    const bool uses_dynamic_link =
                        link_index >= static_cast<std::uint32_t>(max_link_count);
                    const std::uint8_t* link = nullptr;
                    std::uint32_t dynamic_index = 0;
                    if (!uses_dynamic_link)
                    {
                        if (links == nullptr)
                        {
                            scan_complete = false;
                            ++diagnostic_scan_unreadable_links;
                            break;
                        }
                        link = static_cast<const std::uint8_t*>(links) +
                            static_cast<std::uintptr_t>(link_index) * DtLinkStride;
                    }
                    else
                    {
                        dynamic_index = link_index - static_cast<std::uint32_t>(max_link_count);
                        if (!dynamic_links_array_valid || dynamic_links_data == nullptr ||
                            dynamic_index >= static_cast<std::uint32_t>(dynamic_links_size))
                        {
                            scan_complete = false;
                            ++diagnostic_scan_unreadable_links;
                            break;
                        }
                        link = static_cast<const std::uint8_t*>(dynamic_links_data) +
                            static_cast<std::uintptr_t>(dynamic_index) * DtLinkStride;
                    }
                    std::uint64_t reference = 0;
                    std::uint32_t next = 0xffffffff;
                    std::uint8_t edge = 0;
                    std::uint8_t side = 0;
                    std::uint8_t bmin = 0;
                    std::uint8_t bmax = 0;
                    if (!ReadValue(link, 0x00, reference) || !ReadValue(link, 0x08, next) ||
                        !ReadValue(link, 0x0C, edge) || !ReadValue(link, 0x0D, side) ||
                        !ReadValue(link, 0x0E, bmin) || !ReadValue(link, 0x0F, bmax))
                    {
                        scan_complete = false;
                        ++diagnostic_scan_unreadable_links;
                        break;
                    }
                    if (uses_dynamic_link && seen_dynamic_links.insert(link_index).second)
                    {
                        ++diagnostic_dynamic_records_scanned;
                        const auto reason = ClassifyDynamicLinkPlausibility(
                            reference, next, edge, side, bmin, bmax, max_link_count,
                            dynamic_links_size, detour_tile_bits, detour_poly_bits);
                        if (!IsAcceptedDynamicLinkPlausibilityReason(reason))
                        {
                            ++tile_failures;
                            tile_reason_names.insert(
                                DynamicLinkPlausibilityReasonName(reason));
                            record_diagnostic_failure(sample, tile, poly_index, link_index,
                                dynamic_index, max_link_count, dynamic_links_size,
                                dynamic_links_capacity, link, reference, next, edge, side,
                                bmin, bmax, reason);
                        }
                        else if (reason == DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_zero)
                        {
                            ++diagnostic_dynamic_links_valid_edge_zero;
                        }
                        else if (reason == DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_one)
                        {
                            ++diagnostic_dynamic_links_valid_edge_one;
                        }
                        if (reason == DynamicLinkPlausibilityReason::next_out_of_range)
                        {
                            scan_complete = false;
                            break;
                        }
                    }
                    link_index = next;
                }
            }
            if (!scan_complete)
            {
                ++diagnostic_scan_incomplete_tiles;
            }
            if (tile_failures > 0)
            {
                ++diagnostic_dynamic_link_plausibility_failure_tiles;
                diagnostic_dynamic_link_plausibility_failure_max_per_tile =
                    (std::max)(diagnostic_dynamic_link_plausibility_failure_max_per_tile,
                        tile_failures);
                if (tile_failures > 1)
                {
                    ++diagnostic_dynamic_link_plausibility_failure_tiles_multiple;
                }
                if (tile_reason_names.size() > 1)
                {
                    ++diagnostic_dynamic_link_plausibility_failure_tiles_multiple_reasons;
                }
                ++diagnostic_rejection_tile_failure_histogram[
                    static_cast<unsigned int>(tile_failures)];
            }
        };
        for (const auto& entry : inventory.tiles)
        {
            const auto& sample = entry.sample;
            void* tile = sample.tile;
            ++tiles_considered;
            if (exported_tiles >= MaxExportTiles ||
                total_polys + sample.poly_count > MaxTotalPolys ||
                total_vertices + sample.vert_count > MaxTotalVertices)
            {
                truncated = true;
                break;
            }

            void* polys = nullptr;
            void* verts = nullptr;
            void* links = nullptr;
            void* off_mesh_connections = nullptr;
            void* tile_data = nullptr;
            std::int32_t max_link_count = 0;
            std::int32_t tile_data_size = 0;
            std::int32_t off_mesh_count = 0;
            std::int32_t tile_plausibility_failures = 0;
            const bool tile_data_fields_read =
                ReadValue(tile, DtMeshTileDataOffset, tile_data) &&
                ReadValue(tile, DtMeshTileDataSizeOffset, tile_data_size);
            if (!ReadValue(tile, 0x10, polys) || !ReadValue(tile, 0x18, verts) ||
                !ReadValue(tile, 0x20, links) || !ReadValue(tile, 0x48, off_mesh_connections) ||
                !tile_data_fields_read ||
                !ReadValue(sample.header, 0x20, max_link_count) ||
                !ReadValue(sample.header, 0x34, off_mesh_count) ||
                sample.poly_count > 50000 || sample.vert_count > 200000 ||
                max_link_count < 0 || max_link_count > 1000000 ||
                off_mesh_count < 0 || off_mesh_count > MaxOffMeshConnectionsPerTile ||
                (sample.poly_count > 0 && !IsReadable(polys)) ||
                (sample.vert_count > 0 && !IsReadable(verts)))
            {
                ++tiles_skipped_basic;
                continue;
            }
            const auto candidate_a0 = ReadChunkArray(tile, DtMeshTileDynamicLinksOOffset);
            if (candidate_a0.fields_read)
            {
                ++candidate_a0_fields_read;
                if (candidate_a0.structurally_valid)
                {
                    ++candidate_a0_structurally_valid;
                    if (candidate_a0.data == nullptr)
                    {
                        ++candidate_a0_null_data;
                    }
                }
                else
                {
                    ++candidate_a0_invalid_size_capacity;
                }
            }
            else
            {
                ++candidate_a0_unreadable_fields;
            }
            // The v6 +0x98 read is a free-list/sentinel field, not the O-link
            // array. Use the +0xA0 candidate for all dynamic-link resolution.
            void* dynamic_links_data = candidate_a0.data;
            const std::int32_t dynamic_links_size = candidate_a0.size;
            const std::int32_t dynamic_links_capacity = candidate_a0.capacity;
            const bool dynamic_links_fields_read = candidate_a0.fields_read;
            const bool dynamic_links_array_valid = candidate_a0.structurally_valid;
            if (!dynamic_links_fields_read)
            {
                ++dynamic_link_array_read_failures;
                ++dynamic_link_arrays_unreadable_fields;
            }
            else if (!dynamic_links_array_valid)
            {
                ++dynamic_link_array_invalid_rejections;
                ++dynamic_link_arrays_invalid_size_capacity;
            }
            else
            {
                ++dynamic_link_arrays_structurally_valid;
                if (dynamic_links_data == nullptr)
                {
                    ++dynamic_link_arrays_null_data;
                }
            }

            std::vector<Vec3> vertices(static_cast<std::size_t>(sample.vert_count));
            bool tile_complete = true;
            for (std::int32_t vertex_index = 0; vertex_index < sample.vert_count; ++vertex_index)
            {
                auto* vertex = static_cast<const std::uint8_t*>(verts) +
                    static_cast<std::uintptr_t>(vertex_index) * sizeof(Vec3);
                if (!ReadValue(vertex, 0, vertices[static_cast<std::size_t>(vertex_index)]) ||
                    !std::isfinite(vertices[static_cast<std::size_t>(vertex_index)].x) ||
                    !std::isfinite(vertices[static_cast<std::size_t>(vertex_index)].y) ||
                    !std::isfinite(vertices[static_cast<std::size_t>(vertex_index)].z))
                {
                    tile_complete = false;
                    break;
                }
            }
            if (!tile_complete)
            {
                ++tiles_skipped_vertices;
                continue;
            }

            if (sample.poly_count > 1 &&
                shipping_poly_block_diagnostic_samples.size() < MaxMemoryDiagnosticTiles)
            {
                ++shipping_poly_block_tiles_sampled;
                const auto prefix_limit = (std::min)(
                    sample.poly_count, static_cast<std::int32_t>(MaxMemoryDiagnosticPolygons));
                bool sampled_prefixes_all_valid = true;
                bool sampled_link_refs_all_plausible = true;
                std::ostringstream diagnostic;
                diagnostic << "{\"tile_pointer\":\"" << Hex(tile)
                           << "\",\"tile_header_pointer\":\"" << Hex(sample.header)
                           << "\",\"tile_data_pointer\":\"" << Hex(tile_data)
                           << "\",\"tile_data_size\":" << tile_data_size
                           << ",\"polys_pointer\":\"" << Hex(polys)
                           << "\",\"links_pointer\":\"" << Hex(links)
                           << "\",\"poly_count\":" << sample.poly_count
                           << ",\"vert_count\":" << sample.vert_count
                           << ",\"max_link_count\":" << max_link_count
                           << ",\"candidate_block_stride\":"
                           << ShippingPolyRecordStride
                           << ",\"prefix_limit\":" << prefix_limit
                           << ",\"prefixes\":[";
                const auto polys_address = reinterpret_cast<std::uintptr_t>(polys);
                const auto links_address = reinterpret_cast<std::uintptr_t>(links);
                const bool candidate_poly_range_address_valid =
                    polys_address <= (std::numeric_limits<std::uintptr_t>::max)() -
                        static_cast<std::uintptr_t>(sample.poly_count) *
                            ShippingPolyRecordStride;
                const auto candidate_poly_range_end = candidate_poly_range_address_valid
                    ? polys_address + static_cast<std::uintptr_t>(sample.poly_count) *
                        ShippingPolyRecordStride
                    : 0;
                const bool candidate_link_range_address_valid =
                    links_address <= (std::numeric_limits<std::uintptr_t>::max)() -
                        static_cast<std::uintptr_t>(sample.poly_count) * DtLinkStride;
                const bool candidate_poly_range_in_payload =
                    candidate_poly_range_address_valid && tile_data_fields_read &&
                    tile_data != nullptr && tile_data_size >= 0 &&
                    RangeWithin(tile_data, static_cast<std::size_t>(tile_data_size),
                        polys, static_cast<std::size_t>(sample.poly_count) *
                            ShippingPolyRecordStride);
                const bool candidate_link_range_in_payload =
                    candidate_link_range_address_valid && tile_data_fields_read &&
                    tile_data != nullptr && tile_data_size >= 0 &&
                    RangeWithin(tile_data, static_cast<std::size_t>(tile_data_size),
                        links, static_cast<std::size_t>(sample.poly_count) * DtLinkStride);
                const auto expected_links_address = candidate_poly_range_address_valid
                    ? candidate_poly_range_end
                    : 0;
                const bool links_pointer_matches = candidate_poly_range_address_valid &&
                    reinterpret_cast<std::uintptr_t>(links) == expected_links_address;
                const bool payload_bounds = candidate_poly_range_in_payload &&
                    candidate_link_range_in_payload;
                for (std::int32_t poly_index = 0; poly_index < prefix_limit; ++poly_index)
                {
                    if (poly_index != 0)
                    {
                        diagnostic << ",";
                    }
                    ++shipping_poly_block_prefixes_sampled;
                    const auto prefix = ReadDtPolyPrefix(
                        polys, poly_index, ShippingPolyRecordStride);
                    const auto polygon_type = static_cast<std::uint8_t>(prefix.area_and_type >> 6);
                    const bool first_link_plausible = prefix.first_link == 0xffffffff ||
                        prefix.first_link < static_cast<std::uint32_t>(max_link_count) ||
                        (candidate_a0.structurally_valid && candidate_a0.data != nullptr &&
                         prefix.first_link - static_cast<std::uint32_t>(max_link_count) <
                             static_cast<std::uint32_t>(candidate_a0.size));
                    const bool valid = IsValidDtPolyPrefix(
                        prefix, sample.vert_count, max_link_count, candidate_a0);
                    if (valid)
                    {
                        ++shipping_poly_block_prefixes_valid;
                    }
                    else
                    {
                        sampled_prefixes_all_valid = false;
                    }
                    if (first_link_plausible)
                    {
                        ++shipping_poly_block_prefixes_link_ref_plausible;
                    }
                    else
                    {
                        sampled_link_refs_all_plausible = false;
                    }
                    diagnostic << "{\"index\":" << poly_index
                               << ",\"address\":\""
                               << Hex(static_cast<const std::uint8_t*>(polys) +
                                     static_cast<std::uintptr_t>(poly_index) *
                                         ShippingPolyRecordStride)
                               << "\",\"fields_read\":"
                               << (prefix.fields_read ? "true" : "false")
                               << ",\"first_link\":" << prefix.first_link
                               << ",\"first_link_plausible\":"
                               << (first_link_plausible ? "true" : "false")
                               << ",\"link_ref_plausible\":"
                               << (first_link_plausible ? "true" : "false")
                               << ",\"vertex_count\":"
                               << static_cast<unsigned int>(prefix.vertex_count)
                               << ",\"area_and_type\":"
                               << static_cast<unsigned int>(prefix.area_and_type)
                               << ",\"type\":" << static_cast<unsigned int>(polygon_type)
                               << ",\"flags\":" << prefix.flags
                               << ",\"flags_plausible\":"
                               << (prefix.flags != 0 ? "true" : "false")
                               << ",\"neighbors_plausible\":true"
                               << ",\"valid\":" << (valid ? "true" : "false")
                               << ",\"raw_0x20\":"
                               << JsonRawByteDump(ReadRawBytes(
                                   static_cast<const std::uint8_t*>(polys) +
                                       static_cast<std::uintptr_t>(poly_index) *
                                           ShippingPolyRecordStride,
                                   DtPolyStride))
                               << "}";
                }
                if (links_pointer_matches)
                {
                    ++shipping_poly_block_links_pointer_matches;
                }
                else
                {
                    ++shipping_poly_block_links_pointer_mismatches;
                }
                if (sampled_prefixes_all_valid)
                {
                    ++shipping_poly_block_tiles_all_prefixes_valid;
                }
                if (prefix_limit > 0 && sampled_link_refs_all_plausible)
                {
                    ++shipping_poly_block_tiles_all_link_refs_plausible;
                }
                if (payload_bounds)
                {
                    ++shipping_poly_block_payload_bounds_valid;
                }
                else
                {
                    ++shipping_poly_block_payload_bounds_invalid;
                }
                diagnostic << "],\"links_expected_pointer\":\""
                           << Hex(reinterpret_cast<void*>(expected_links_address))
                           << "\",\"links_pointer_matches\":"
                           << (links_pointer_matches ? "true" : "false")
                           << ",\"payload_bounds\":{\"candidate_poly_range_in_payload\":"
                           << (candidate_poly_range_in_payload ? "true" : "false")
                           << ",\"candidate_link_range_in_payload\":"
                           << (candidate_link_range_in_payload ? "true" : "false")
                           << ",\"valid\":" << (payload_bounds ? "true" : "false") << "}"
                           << ",\"all_sampled_prefixes_valid\":"
                           << (sampled_prefixes_all_valid ? "true" : "false")
                           << ",\"all_sampled_link_refs_plausible\":"
                           << (prefix_limit > 0 && sampled_link_refs_all_plausible
                               ? "true" : "false")
                           << "}";
                shipping_poly_block_diagnostic_samples.push_back(diagnostic.str());
            }

            bool candidate_a0_tile_success = false;
            bool tile_has_incomplete_link_data = false;
            std::uint32_t tile_index = 0;
            bool tile_index_valid = false;
            if (detour_tiles != nullptr && sample.tile != nullptr)
            {
                const auto tiles_base = reinterpret_cast<std::uintptr_t>(detour_tiles);
                const auto tile_address = reinterpret_cast<std::uintptr_t>(sample.tile);
                if (tile_address >= tiles_base &&
                    (tile_address - tiles_base) % DtMeshTileStride == 0)
                {
                    const auto index = (tile_address - tiles_base) / DtMeshTileStride;
                    if (index < static_cast<std::uintptr_t>(detour_max_tiles) &&
                        index < (std::uint32_t{1} << 14))
                    {
                        tile_index = static_cast<std::uint32_t>(index);
                        tile_index_valid = true;
                    }
                }
            }
            std::ostringstream tile_output;
                tile_output << "{\"lookup_index\":" << sample.lookup_index
                            << ",\"tile_index\":" << tile_index
                            << ",\"tile_index_valid\":" << (tile_index_valid ? "true" : "false")
                            << ",\"salt\":" << sample.salt
                            << ",\"x\":" << sample.x
                            << ",\"y\":" << sample.y
                            << ",\"layer\":" << sample.layer
                            << ",\"poly_count\":" << sample.poly_count
                            << ",\"vert_count\":" << sample.vert_count
                            << ",\"max_link_count\":" << max_link_count
                            << ",\"vertices\":[";
                for (std::size_t vertex_index = 0; vertex_index < vertices.size(); ++vertex_index)
                {
                    if (vertex_index != 0) tile_output << ",";
                    tile_output << JsonVec(vertices[vertex_index]);
                }
                tile_output << "],\"polygons\":[";
                bool first_poly = true;
                for (std::int32_t poly_index = 0; poly_index < sample.poly_count; ++poly_index)
                {
                    auto* poly = static_cast<const std::uint8_t*>(polys) +
                        static_cast<std::uintptr_t>(poly_index) * ShippingPolyRecordStride;
                    std::uint32_t first_link = 0xffffffff;
                    std::array<std::uint16_t, 6> poly_vertices{};
                    std::array<std::uint16_t, 6> poly_neighbors{};
                    std::uint16_t flags = 0;
                    std::uint8_t vertex_count = 0;
                    std::uint8_t area_and_type = 0;
                    bool polygon_fields_valid = true;
                    if (!ReadValue(poly, 0x00, first_link))
                    {
                        ++polygon_first_link_read_failures;
                        polygon_fields_valid = false;
                    }
                    if (!ReadValue(poly, 0x04, poly_vertices))
                    {
                        ++polygon_vertex_array_read_failures;
                        polygon_fields_valid = false;
                    }
                    if (!ReadValue(poly, 0x10, poly_neighbors))
                    {
                        ++polygon_neighbor_array_read_failures;
                        polygon_fields_valid = false;
                    }
                    if (!ReadValue(poly, 0x1C, flags))
                    {
                        ++polygon_flags_read_failures;
                        polygon_fields_valid = false;
                    }
                    if (!ReadValue(poly, 0x1E, vertex_count))
                    {
                        ++polygon_vertex_count_read_failures;
                        polygon_fields_valid = false;
                    }
                    if (!ReadValue(poly, 0x1F, area_and_type))
                    {
                        ++polygon_area_type_read_failures;
                        polygon_fields_valid = false;
                    }
                    const auto polygon_type = static_cast<std::uint8_t>(area_and_type >> 6);
                    const bool valid_vertex_count =
                        (polygon_type == DetourPolyTypeGround && vertex_count >= 3 && vertex_count <= 6) ||
                        (polygon_type == DetourPolyTypeOffMeshPoint && vertex_count == 2) ||
                        (polygon_type == DetourPolyTypeOffMeshSegment && vertex_count >= 3 && vertex_count <= 6);
                    if (!valid_vertex_count)
                    {
                        ++polygon_vertex_count_invalid;
                        polygon_fields_valid = false;
                    }
                    else if (polygon_type != DetourPolyTypeGround)
                    {
                        ++polygon_off_mesh_vertices_accepted;
                    }
                    if (!polygon_fields_valid)
                    {
                        ++polygon_read_failures;
                        if (rejection_samples.size() < 32)
                        {
                            std::ostringstream rejection;
                            rejection << "{\"kind\":\"polygon\",\"lookup_index\":"
                                       << sample.lookup_index << ",\"poly_index\":" << poly_index
                                       << ",\"first_link\":" << first_link
                                       << ",\"vertex_count\":" << static_cast<unsigned int>(vertex_count)
                                       << ",\"area_and_type\":" << static_cast<unsigned int>(area_and_type)
                                       << ",\"type\":" << static_cast<unsigned int>(polygon_type) << "}";
                            rejection_samples.push_back(rejection.str());
                        }
                        if (sample.poly_count > 1 &&
                            memory_diagnostic_samples.size() < MaxMemoryDiagnosticTiles)
                        {
                            const auto expected_polys_offset =
                                DtMeshHeaderStride +
                                static_cast<std::uintptr_t>(sample.vert_count) * DtRecastVertexStride;
                            const auto expected_polys_end_offset =
                                expected_polys_offset +
                                static_cast<std::uintptr_t>(sample.poly_count) *
                                    ShippingPolyRecordStride;
                            const bool expected_payload_bounds = tile_data_fields_read &&
                                tile_data != nullptr && tile_data_size >= 0 &&
                                expected_polys_end_offset <= static_cast<std::uintptr_t>(tile_data_size);
                            std::uintptr_t expected_polys_address = 0;
                            if (tile_data != nullptr && expected_polys_offset <=
                                (std::numeric_limits<std::uintptr_t>::max)() -
                                    reinterpret_cast<std::uintptr_t>(tile_data))
                            {
                                expected_polys_address = reinterpret_cast<std::uintptr_t>(tile_data) +
                                    expected_polys_offset;
                            }
                            const bool pointer_comparable = expected_polys_address != 0 && polys != nullptr;
                            const char* pointer_classification = "INSUFFICIENT_EVIDENCE";
                            std::int64_t runtime_polys_delta = 0;
                            if (pointer_comparable)
                            {
                                pointer_classification =
                                    reinterpret_cast<std::uintptr_t>(polys) == expected_polys_address
                                        ? "POLY_POINTER_CONFIRMED"
                                        : "POLY_POINTER_OFFSET_WRONG";
                                const auto runtime_address = reinterpret_cast<std::uintptr_t>(polys);
                                runtime_polys_delta = runtime_address >= expected_polys_address
                                    ? static_cast<std::int64_t>(runtime_address - expected_polys_address)
                                    : -static_cast<std::int64_t>(expected_polys_address - runtime_address);
                            }
                            const auto transition_address = reinterpret_cast<std::uintptr_t>(poly);
                            const auto transition_window_start = transition_address >= 0x20
                                ? transition_address - 0x20
                                : transition_address;
                            std::ostringstream diagnostic;
                            diagnostic << "{\"tile_pointer\":\"" << Hex(tile)
                                       << "\",\"tile_header_pointer\":\"" << Hex(sample.header)
                                       << "\",\"tile_data_pointer\":\"" << Hex(tile_data)
                                       << "\",\"tile_data_size\":" << tile_data_size
                                       << ",\"polys_pointer\":\"" << Hex(polys)
                                       << "\",\"verts_pointer\":\"" << Hex(verts)
                                       << "\",\"links_pointer\":\"" << Hex(links)
                                       << "\",\"header\":{\"poly_count\":" << sample.poly_count
                                       << ",\"vert_count\":" << sample.vert_count
                                       << ",\"max_link_count\":" << max_link_count
                                       << ",\"off_mesh_count\":" << off_mesh_count
                                       << ",\"raw_0x40\":"
                                       << JsonRawByteDump(ReadRawBytes(sample.header, DtMeshHeaderStride))
                                       << "}"
                                       << ",\"serialized_layout\":{\"header_stride\":"
                                       << DtMeshHeaderStride
                                       << ",\"vertex_stride\":" << DtRecastVertexStride
                                       << ",\"source_poly_stride\":" << DtPolyStride
                                       << ",\"shipping_block_stride_candidate\":"
                                       << ShippingPolyRecordStride
                                       << ",\"expected_polys_offset\":" << expected_polys_offset
                                       << ",\"expected_polys_end_offset\":" << expected_polys_end_offset
                                       << ",\"expected_polys_pointer\":\""
                                       << Hex(reinterpret_cast<void*>(expected_polys_address))
                                       << "\",\"expected_payload_bounds\":"
                                       << (expected_payload_bounds ? "true" : "false")
                                       << ",\"pointer_classification\":\""
                                       << pointer_classification
                                       << "\",\"runtime_polys_delta\":" << runtime_polys_delta << "}"
                                       << ",\"candidate_chunk_array_a0\":{\"data\":\""
                                       << Hex(candidate_a0.data)
                                       << "\",\"size\":" << candidate_a0.size
                                       << ",\"capacity\":" << candidate_a0.capacity
                                       << ",\"fields_read\":"
                                       << (candidate_a0.fields_read ? "true" : "false")
                                       << ",\"array_valid\":"
                                       << (candidate_a0.structurally_valid ? "true" : "false") << "}"
                                       << ",\"dynamic_links_candidate_a0\":{\"data\":\""
                                       << Hex(dynamic_links_data)
                                       << "\",\"size\":" << dynamic_links_size
                                       << ",\"capacity\":" << dynamic_links_capacity
                                       << ",\"fields_read\":"
                                       << (dynamic_links_fields_read ? "true" : "false")
                                       << ",\"array_valid\":"
                                       << (dynamic_links_array_valid ? "true" : "false") << "}"
                                       << ",\"tile_object_raw_0xb0\":"
                                       << JsonRawByteDump(ReadRawBytes(tile, 0xb0))
                                       << ",\"transition\":{\"poly_index\":" << poly_index
                                       << ",\"address\":\"" << Hex(poly)
                                       << "\",\"first_link\":" << first_link
                                       << ",\"vertex_count\":" << static_cast<unsigned int>(vertex_count)
                                       << ",\"area_and_type\":" << static_cast<unsigned int>(area_and_type)
                                       << ",\"raw_window_start\":\""
                                       << Hex(reinterpret_cast<void*>(transition_window_start))
                                       << "\",\"raw_window\":"
                                       << JsonRawByteDump(ReadRawBytes(
                                            reinterpret_cast<void*>(transition_window_start),
                                            PolygonDiagnosticWindowBytes))
                                       << "}"
                                       << ",\"raw_polygon_records\":[";
                            const auto raw_polygon_count = (std::min)(
                                sample.poly_count, static_cast<std::int32_t>(MaxMemoryDiagnosticPolygons));
                            for (std::int32_t raw_index = 0; raw_index < raw_polygon_count; ++raw_index)
                            {
                                if (raw_index != 0) diagnostic << ",";
                                diagnostic << JsonRawPolygonRecord(
                                    polys, raw_index, ShippingPolyRecordStride);
                            }
                            diagnostic << "]}";
                            memory_diagnostic_samples.push_back(diagnostic.str());
                        }
                        tile_complete = false;
                        break;
                    }
                    for (std::uint8_t vertex_index = 0; vertex_index < vertex_count; ++vertex_index)
                    {
                        if (poly_vertices[vertex_index] >= sample.vert_count)
                        {
                            ++polygon_vertex_index_failures;
                            tile_complete = false;
                            break;
                        }
                    }
                    if (!tile_complete)
                    {
                        break;
                    }
                    if (!first_poly) tile_output << ",";
                    first_poly = false;
                    tile_output << "{\"index\":" << poly_index
                                << ",\"first_link\":" << first_link
                                << ",\"vertex_count\":" << static_cast<unsigned int>(vertex_count)
                                << ",\"vertex_indices\":[";
                    for (std::uint8_t vertex_index = 0; vertex_index < vertex_count; ++vertex_index)
                    {
                        if (vertex_index != 0) tile_output << ",";
                        tile_output << poly_vertices[vertex_index];
                    }
                    tile_output << "],\"neighbors\":[";
                    for (std::size_t neighbor_index = 0; neighbor_index < poly_neighbors.size(); ++neighbor_index)
                    {
                        if (neighbor_index != 0) tile_output << ",";
                        tile_output << poly_neighbors[neighbor_index];
                    }
                    tile_output << "],\"flags\":" << flags
                                << ",\"area\":" << static_cast<unsigned int>(area_and_type & 0x3f)
                                << ",\"type\":" << static_cast<unsigned int>(area_and_type >> 6)
                                << ",\"links\":[";
                    std::uint32_t link_index = first_link;
                    bool first_link_output = true;
                    bool link_chain_rejected = false;
                    std::int32_t link_hops = 0;
                    for (; link_index != 0xffffffff && link_hops < MaxLinksPerPoly; ++link_hops)
                    {
                        const bool uses_dynamic_link =
                            link_index >= static_cast<std::uint32_t>(max_link_count);
                        const std::uint8_t* link = nullptr;
                        std::uint32_t dynamic_index = 0;
                        if (!uses_dynamic_link)
                        {
                            if (links == nullptr)
                            {
                                ++link_array_null_rejections;
                                ++link_pointer_rejections;
                                link_chain_rejected = true;
                                tile_has_incomplete_link_data = true;
                                ++polygons_with_incomplete_link_data;
                                tile_complete = false;
                                break;
                            }
                            link = static_cast<const std::uint8_t*>(links) +
                                static_cast<std::uintptr_t>(link_index) * DtLinkStride;
                        }
                        else
                        {
                            ++candidate_a0_link_attempts;
                            dynamic_index = link_index -
                                static_cast<std::uint32_t>(max_link_count);
                            if (!dynamic_links_array_valid)
                            {
                                ++candidate_a0_link_array_rejections;
                                ++dynamic_link_array_invalid_rejections;
                                ++link_pointer_rejections;
                                link_chain_rejected = true;
                                tile_has_incomplete_link_data = true;
                                ++polygons_with_incomplete_link_data;
                                tile_complete = false;
                                break;
                            }
                            if (dynamic_links_data == nullptr)
                            {
                                ++candidate_a0_link_data_rejections;
                                ++dynamic_link_array_null_rejections;
                                ++link_pointer_rejections;
                                link_chain_rejected = true;
                                tile_has_incomplete_link_data = true;
                                ++polygons_with_incomplete_link_data;
                                tile_complete = false;
                                break;
                            }
                            if (dynamic_index >= static_cast<std::uint32_t>(dynamic_links_size))
                            {
                                ++candidate_a0_link_index_rejections;
                                ++link_index_out_of_range;
                                ++dynamic_link_index_out_of_range;
                                ++link_pointer_rejections;
                                if (rejection_samples.size() < 32)
                                {
                                    std::ostringstream rejection;
                                    rejection << "{\"kind\":\"dynamic_link_index\",\"lookup_index\":"
                                               << sample.lookup_index << ",\"poly_index\":" << poly_index
                                               << ",\"link_index\":" << link_index
                                               << ",\"max_link_count\":" << max_link_count
                                               << ",\"dynamic_index\":" << dynamic_index
                                               << ",\"dynamic_links_size\":" << dynamic_links_size << "}";
                                    rejection_samples.push_back(rejection.str());
                                }
                                link_chain_rejected = true;
                                tile_has_incomplete_link_data = true;
                                ++polygons_with_incomplete_link_data;
                                tile_complete = false;
                                break;
                            }
                            link = static_cast<const std::uint8_t*>(dynamic_links_data) +
                                static_cast<std::uintptr_t>(dynamic_index) * DtLinkStride;
                            ++dynamic_links_used;
                        }
                        std::uint64_t reference = 0;
                        std::uint32_t next = 0xffffffff;
                        std::uint8_t edge = 0;
                        std::uint8_t side = 0;
                        std::uint8_t bmin = 0;
                        std::uint8_t bmax = 0;
                        if (!ReadValue(link, 0x00, reference) || !ReadValue(link, 0x08, next) ||
                            !ReadValue(link, 0x0C, edge) || !ReadValue(link, 0x0D, side) ||
                            !ReadValue(link, 0x0E, bmin) || !ReadValue(link, 0x0F, bmax))
                        {
                            if (uses_dynamic_link)
                            {
                                ++candidate_a0_link_read_failures;
                            }
                            ++link_read_failures;
                            link_chain_rejected = true;
                            tile_has_incomplete_link_data = true;
                            ++polygons_with_incomplete_link_data;
                            tile_complete = false;
                            break;
                        }
                        if (uses_dynamic_link)
                        {
                            const auto plausibility_reason = ClassifyDynamicLinkPlausibility(
                                reference, next, edge, side, bmin, bmax, max_link_count,
                                dynamic_links_size, detour_tile_bits, detour_poly_bits);
                            if (!IsAcceptedDynamicLinkPlausibilityReason(plausibility_reason))
                            {
                                ++candidate_a0_link_plausibility_failures;
                                ++tile_plausibility_failures;
                                const auto reason_name = DynamicLinkPlausibilityReasonName(
                                    plausibility_reason);
                                ++candidate_a0_link_rejection_reason_counts[reason_name];
                                ++candidate_a0_link_rejection_edge_counts[
                                    static_cast<unsigned int>(edge)];
                                ++candidate_a0_link_rejection_side_counts[
                                    static_cast<unsigned int>(side)];
                                ++candidate_a0_link_rejection_edge_side_counts[
                                    (static_cast<unsigned int>(edge) << 8) |
                                    static_cast<unsigned int>(side)];
                                if (candidate_a0_link_diagnostic_samples.size() <
                                    MaxDynamicLinkPlausibilityDiagnosticSamples)
                                {
                                    const auto decoded = DecodeDynamicLinkReference(
                                        reference, detour_tile_bits, detour_poly_bits);
                                    std::uint32_t source_tile_index = 0;
                                    const bool source_tile_index_valid =
                                        tile_index_for_pointer(tile, source_tile_index) &&
                                        source_tile_index <= 0x3fff;
                                    const auto raw = ReadRawBytes(link, DtLinkStride);
                                    std::ostringstream diagnostic;
                                    diagnostic << "{\"kind\":\"dynamic_link_plausibility\""
                                               << ",\"reason\":\"" << reason_name << "\""
                                               << ",\"lookup_index\":" << sample.lookup_index
                                               << ",\"source_tile_index\":";
                                    if (source_tile_index_valid) diagnostic << source_tile_index;
                                    else diagnostic << "null";
                                    diagnostic << ",\"source_tile_index_valid\":"
                                               << (source_tile_index_valid ? "true" : "false")
                                               << ",\"source_salt\":" << sample.salt
                                               << ",\"poly_index\":" << poly_index
                                               << ",\"global_link_index\":" << link_index
                                               << ",\"link_storage\":\"dynamic\""
                                               << ",\"dynamic_index\":" << dynamic_index
                                               << ",\"base_count\":" << max_link_count
                                               << ",\"dynamic_count\":" << dynamic_links_size
                                               << ",\"dynamic_capacity\":" << dynamic_links_capacity
                                               << ",\"link_address\":\"" << Hex(link) << "\""
                                               << ",\"reference\":" << reference
                                               << ",\"decoded_target\":";
                                    if (!decoded.valid)
                                    {
                                        diagnostic << "null";
                                    }
                                    else
                                    {
                                        diagnostic << "{\"valid\":true,\"salt\":"
                                                   << decoded.salt
                                                   << ",\"tile_index\":" << decoded.tile_index
                                                   << ",\"poly_index\":" << decoded.poly_index
                                                   << "}";
                                    }
                                    diagnostic << ",\"next\":" << next
                                               << ",\"edge\":"
                                               << static_cast<unsigned int>(edge)
                                               << ",\"side\":"
                                               << static_cast<unsigned int>(side)
                                               << ",\"bmin\":"
                                               << static_cast<unsigned int>(bmin)
                                               << ",\"bmax\":"
                                               << static_cast<unsigned int>(bmax)
                                               << ",\"raw_0x10\":"
                                               << JsonRawByteDump(raw)
                                               << ",\"raw_16_hex\":\"" << raw.hex << "\"}";
                                    candidate_a0_link_diagnostic_samples.push_back(diagnostic.str());
                                }
                                ++link_pointer_rejections;
                                link_chain_rejected = true;
                                tile_has_incomplete_link_data = true;
                                ++polygons_with_incomplete_link_data;
                                tile_complete = false;
                                break;
                            }
                            if (plausibility_reason ==
                                DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_zero)
                            {
                                ++dynamic_links_valid_edge_zero;
                                ++candidate_a0_link_valid_edge_zero;
                            }
                            else if (plausibility_reason ==
                                DynamicLinkPlausibilityReason::valid_shipping_dynamic_edge_one)
                            {
                                ++dynamic_links_valid_edge_one;
                                ++candidate_a0_link_valid_edge_one;
                            }
                        }
                        if (uses_dynamic_link)
                        {
                            ++dynamic_links_resolved;
                            ++dynamic_links_route_semantics_unresolved;
                            ++candidate_a0_link_resolved;
                            if (!candidate_a0_tile_success)
                            {
                                candidate_a0_tile_success = true;
                                ++candidate_a0_tiles_with_success;
                            }
                        }
                        if (!first_link_output) tile_output << ",";
                        first_link_output = false;
                        tile_output << "{\"ref\":" << reference
                                    << ",\"next\":" << next
                                    << ",\"edge\":" << static_cast<unsigned int>(edge)
                                    << ",\"side\":" << static_cast<unsigned int>(side)
                                    << ",\"bmin\":" << static_cast<unsigned int>(bmin)
                                    << ",\"bmax\":" << static_cast<unsigned int>(bmax) << "}";
                        link_index = next;
                    }
                    if (!link_chain_rejected && link_index != 0xffffffff)
                    {
                        if (link_hops >= MaxLinksPerPoly)
                        {
                            ++link_chain_bound_hits;
                        }
                        tile_complete = false;
                        break;
                    }
                    tile_output << "]}";
                }
                if (tile_plausibility_failures > 0)
                {
                    scan_diagnostic_tile(sample, tile, polys, links,
                        dynamic_links_array_valid, dynamic_links_data,
                        dynamic_links_size, dynamic_links_capacity, max_link_count);
                }
                if (tile_plausibility_failures > 0)
                {
                    ++candidate_a0_link_plausibility_failure_tiles;
                    candidate_a0_link_plausibility_failure_max_per_tile =
                        (std::max)(candidate_a0_link_plausibility_failure_max_per_tile,
                            tile_plausibility_failures);
                    if (tile_plausibility_failures > 1)
                    {
                        ++candidate_a0_link_plausibility_failure_tiles_multiple;
                        ++candidate_a0_link_rejection_tile_failure_histogram[2];
                    }
                    else
                    {
                        ++candidate_a0_link_rejection_tile_failure_histogram[1];
                    }
                }
                if (!tile_complete)
                {
                    if (tile_has_incomplete_link_data)
                    {
                        ++tiles_with_incomplete_link_data;
                    }
                    ++tiles_skipped_polygons_or_links;
                    continue;
                }
                tile_output << "],\"off_mesh_connections\":[";
                if (off_mesh_count > 0 && off_mesh_connections == nullptr)
                {
                    ++off_mesh_pointer_rejections;
                    ++tiles_skipped_off_mesh;
                    continue;
                }
                for (std::int32_t connection_index = 0; connection_index < off_mesh_count; ++connection_index)
                {
                    auto* connection = static_cast<const std::uint8_t*>(off_mesh_connections) +
                        static_cast<std::uintptr_t>(connection_index) * 0x28;
                    std::array<float, 6> positions{};
                    float radius = 0.0f;
                    float height = 0.0f;
                    std::uint32_t user_id = 0;
                    std::uint16_t poly = 0;
                    std::uint8_t side = 0;
                    std::uint8_t flags = 0;
                    if (!ReadValue(connection, 0x00, positions) ||
                        !ReadValue(connection, 0x18, radius) || !ReadValue(connection, 0x1C, height) ||
                        !ReadValue(connection, 0x20, user_id) || !ReadValue(connection, 0x24, poly) ||
                        !ReadValue(connection, 0x26, side) || !ReadValue(connection, 0x27, flags))
                    {
                        ++off_mesh_read_failures;
                        tile_complete = false;
                        break;
                    }
                    if (connection_index != 0) tile_output << ",";
                    tile_output << "{\"start\":[" << positions[0] << "," << positions[1] << "," << positions[2]
                                << "],\"end\":[" << positions[3] << "," << positions[4] << "," << positions[5]
                                << "],\"radius\":" << radius
                                << ",\"height\":" << height
                                << ",\"user_id\":" << user_id
                                << ",\"poly\":" << poly
                                << ",\"side\":" << static_cast<unsigned int>(side)
                                << ",\"flags\":" << static_cast<unsigned int>(flags) << "}";
                }
                if (!tile_complete)
                {
                    ++tiles_skipped_off_mesh;
                    continue;
                }
            tile_output << "]}";
            if (!first_tile) output << ",";
            first_tile = false;
            output << tile_output.str();
            ++exported_tiles;
            total_polys += sample.poly_count;
            total_vertices += sample.vert_count;
        }
        const auto geometry_stage_skips = tiles_skipped_basic + tiles_skipped_vertices +
            tiles_skipped_polygons_or_links + tiles_skipped_off_mesh;
        const bool exported_tile_count_matches = exported_tiles == inventory.unique_active_tiles;
        const bool candidate_prefix_validation_passed =
            shipping_poly_block_tiles_sampled > 0 &&
            shipping_poly_block_prefixes_sampled > 0 &&
            shipping_poly_block_prefixes_valid == shipping_poly_block_prefixes_sampled &&
            shipping_poly_block_tiles_all_prefixes_valid == shipping_poly_block_tiles_sampled &&
            shipping_poly_block_tiles_all_link_refs_plausible == shipping_poly_block_tiles_sampled &&
            shipping_poly_block_links_pointer_mismatches == 0 &&
            shipping_poly_block_payload_bounds_invalid == 0;
        const bool dynamic_link_candidate_validation_passed =
            candidate_a0_link_attempts > 0 &&
            candidate_a0_link_resolved == candidate_a0_link_attempts &&
            candidate_a0_link_plausibility_failures == 0 &&
            candidate_a0_tiles_with_success >= 2;
        const bool candidate_validation_passed = candidate_prefix_validation_passed &&
            dynamic_link_candidate_validation_passed;
        // Static shipping-binary reconciliation independently proved the
        // polygon indexing stride, dynamicLinksO chunk-array location and
        // global firstLink/next encoding. Live structural checks still gate
        // each exported tile; this does not authorise a production route or
        // a topology proof.
        const bool candidate_layout_independently_validated = ShippingBinaryLayoutProven;
        const bool geometry_complete = inventory.full_lookup_chain_traversed &&
            !truncated && geometry_stage_skips == 0 && exported_tile_count_matches &&
            candidate_validation_passed && candidate_layout_independently_validated;
        geometry_export_complete = geometry_complete;
        const auto topology_proof = BuildTopologyProof(inventory, detour_tiles,
            detour_max_tiles, detour_tile_bits, detour_poly_bits, player_position);
        const char* geometry_reason = inventory.full_lookup_chain_traversed ?
            (truncated ? "geometry_export_bounded" :
             (geometry_stage_skips != 0 ? "geometry_stage_rejections" :
              (!exported_tile_count_matches ? "geometry_tile_count_mismatch" :
               (!candidate_prefix_validation_passed ? "candidate_validation_failed" :
                (!dynamic_link_candidate_validation_passed ? "candidate_link_layout_unproven" :
                 (!candidate_layout_independently_validated
                      ? "candidate_layout_independent_proof_required"
                      : "geometry_export_complete")))))) :
            "lookup_chain_incomplete";
        output << "],\"tiles_exported\":" << exported_tiles
               << ",\"total_polygons\":" << total_polys
               << ",\"total_vertices\":" << total_vertices
               << ",\"tiles_considered\":" << tiles_considered
               << ",\"valid_unique_tiles\":" << inventory.unique_active_tiles
               << ",\"exported_unique_tiles\":" << exported_tiles
               << ",\"tiles_skipped_basic\":" << tiles_skipped_basic
               << ",\"tiles_skipped_vertices\":" << tiles_skipped_vertices
               << ",\"tiles_skipped_polygons_or_links\":" << tiles_skipped_polygons_or_links
               << ",\"tiles_skipped_off_mesh\":" << tiles_skipped_off_mesh
               << ",\"polygon_read_failures\":" << polygon_read_failures
               << ",\"polygon_first_link_read_failures\":" << polygon_first_link_read_failures
               << ",\"polygon_vertex_array_read_failures\":" << polygon_vertex_array_read_failures
               << ",\"polygon_neighbor_array_read_failures\":" << polygon_neighbor_array_read_failures
               << ",\"polygon_flags_read_failures\":" << polygon_flags_read_failures
               << ",\"polygon_vertex_count_read_failures\":" << polygon_vertex_count_read_failures
               << ",\"polygon_area_type_read_failures\":" << polygon_area_type_read_failures
               << ",\"polygon_vertex_count_invalid\":" << polygon_vertex_count_invalid
               << ",\"polygon_off_mesh_vertices_accepted\":" << polygon_off_mesh_vertices_accepted
               << ",\"polygon_vertex_index_failures\":" << polygon_vertex_index_failures
               << ",\"link_pointer_rejections\":" << link_pointer_rejections
               << ",\"link_array_null_rejections\":" << link_array_null_rejections
               << ",\"link_index_out_of_range\":" << link_index_out_of_range
               << ",\"link_read_failures\":" << link_read_failures
               << ",\"link_chain_bound_hits\":" << link_chain_bound_hits
               << ",\"dynamic_link_array_read_failures\":" << dynamic_link_array_read_failures
               << ",\"dynamic_link_array_invalid_rejections\":" << dynamic_link_array_invalid_rejections
               << ",\"dynamic_link_array_null_rejections\":" << dynamic_link_array_null_rejections
               << ",\"dynamic_link_index_out_of_range\":" << dynamic_link_index_out_of_range
               << ",\"dynamic_links_used\":" << dynamic_links_used
               << ",\"dynamic_link_arrays_structurally_valid\":" << dynamic_link_arrays_structurally_valid
               << ",\"dynamic_link_arrays_null_data\":" << dynamic_link_arrays_null_data
               << ",\"dynamic_link_arrays_invalid_size_capacity\":" << dynamic_link_arrays_invalid_size_capacity
               << ",\"dynamic_link_arrays_unreadable_fields\":" << dynamic_link_arrays_unreadable_fields
               << ",\"dynamic_links_resolved\":" << dynamic_links_resolved
               << ",\"dynamic_links_route_semantics_unresolved\":" << dynamic_links_route_semantics_unresolved
               << ",\"dynamic_links_valid_edge_zero\":" << dynamic_links_valid_edge_zero
               << ",\"dynamic_links_valid_edge_one\":" << dynamic_links_valid_edge_one
               << ",\"candidate_a0_fields_read\":" << candidate_a0_fields_read
               << ",\"candidate_a0_structurally_valid\":" << candidate_a0_structurally_valid
               << ",\"candidate_a0_null_data\":" << candidate_a0_null_data
               << ",\"candidate_a0_invalid_size_capacity\":" << candidate_a0_invalid_size_capacity
               << ",\"candidate_a0_unreadable_fields\":" << candidate_a0_unreadable_fields
               << ",\"candidate_a0_link_attempts\":" << candidate_a0_link_attempts
               << ",\"candidate_a0_link_resolved\":" << candidate_a0_link_resolved
               << ",\"candidate_a0_link_valid_edge_zero\":" << candidate_a0_link_valid_edge_zero
               << ",\"candidate_a0_link_valid_edge_one\":" << candidate_a0_link_valid_edge_one
               << ",\"candidate_a0_link_data_rejections\":" << candidate_a0_link_data_rejections
               << ",\"candidate_a0_link_array_rejections\":" << candidate_a0_link_array_rejections
               << ",\"candidate_a0_link_index_rejections\":" << candidate_a0_link_index_rejections
               << ",\"candidate_a0_link_read_failures\":" << candidate_a0_link_read_failures
               << ",\"candidate_a0_link_plausibility_failures\":" << candidate_a0_link_plausibility_failures;
        output << ",\"candidate_a0_link_plausibility_first_failure_tiles\":"
               << candidate_a0_link_plausibility_failure_tiles
               << ",\"candidate_a0_link_plausibility_failures_scope\":\"production_first_failure_only\""
               << ",\"candidate_a0_link_plausibility_first_failure_tiles_multiple\":"
               << candidate_a0_link_plausibility_failure_tiles_multiple
               << ",\"candidate_a0_link_plausibility_first_failure_max_per_tile\":"
               << candidate_a0_link_plausibility_failure_max_per_tile
               << ",\"candidate_a0_link_first_rejection_reason_counts\":";
        append_string_count_object(candidate_a0_link_rejection_reason_counts);
        output << ",\"candidate_a0_link_first_rejection_edge_counts\":";
        append_numeric_count_object(candidate_a0_link_rejection_edge_counts);
        output << ",\"candidate_a0_link_first_rejection_side_counts\":";
        append_numeric_count_object(candidate_a0_link_rejection_side_counts);
        output << ",\"candidate_a0_link_first_rejection_edge_side_counts\":";
        append_edge_side_count_object(candidate_a0_link_rejection_edge_side_counts);
        output << ",\"candidate_a0_link_first_rejection_tile_failure_histogram\":";
        append_numeric_count_object(candidate_a0_link_rejection_tile_failure_histogram);
        output << ",\"diagnostic_scan_tiles\":" << diagnostic_scan_tiles
               << ",\"diagnostic_scan_incomplete_tiles\":"
               << diagnostic_scan_incomplete_tiles
               << ",\"diagnostic_scan_bound_hits\":" << diagnostic_scan_bound_hits
               << ",\"diagnostic_scan_unreadable_links\":"
               << diagnostic_scan_unreadable_links
               << ",\"diagnostic_dynamic_records_scanned\":"
               << diagnostic_dynamic_records_scanned
               << ",\"diagnostic_dynamic_link_plausibility_failures\":"
               << diagnostic_dynamic_link_plausibility_failures
               << ",\"diagnostic_dynamic_link_plausibility_failure_tiles\":"
               << diagnostic_dynamic_link_plausibility_failure_tiles
               << ",\"diagnostic_dynamic_link_plausibility_failure_tiles_multiple\":"
               << diagnostic_dynamic_link_plausibility_failure_tiles_multiple
               << ",\"diagnostic_dynamic_link_plausibility_failure_tiles_multiple_reasons\":"
               << diagnostic_dynamic_link_plausibility_failure_tiles_multiple_reasons
               << ",\"diagnostic_dynamic_link_plausibility_failure_max_per_tile\":"
               << diagnostic_dynamic_link_plausibility_failure_max_per_tile
               << ",\"diagnostic_dynamic_links_valid_edge_zero\":"
               << diagnostic_dynamic_links_valid_edge_zero
               << ",\"diagnostic_dynamic_links_valid_edge_one\":"
               << diagnostic_dynamic_links_valid_edge_one
               << ",\"diagnostic_rejection_reason_counts\":";
        append_string_count_object(diagnostic_rejection_reason_counts);
        output << ",\"diagnostic_rejection_edge_counts\":";
        append_numeric_count_object(diagnostic_rejection_edge_counts);
        output << ",\"diagnostic_rejection_side_counts\":";
        append_numeric_count_object(diagnostic_rejection_side_counts);
        output << ",\"diagnostic_rejection_edge_side_counts\":";
        append_edge_side_count_object(diagnostic_rejection_edge_side_counts);
        output << ",\"diagnostic_rejection_tile_failure_histogram\":";
        append_numeric_count_object(diagnostic_rejection_tile_failure_histogram);
        output << ",\"diagnostic_scan_scope\":\"affected_tiles_all_readable_distinct_dynamic_records\""
               << ",\"diagnostic_scan_complete\":"
               << ((diagnostic_scan_incomplete_tiles == 0 && diagnostic_scan_bound_hits == 0)
                   ? "true" : "false")
               << ",\"candidate_a0_plausibility_predicates\":{\"reference\":\"shipping_ref_"
               << detour_tile_bits << "_tile_bits_" << detour_poly_bits
               << "_poly_bits_nonzero_salt\",\"next\":\"global_index_within_base_or_dynamic_array\""
               << ",\"shipping_dynamic_edges\":[0,1,255],\"shipping_dynamic_edge_zero_sides\":[208,240]"
               << ",\"shipping_dynamic_edge_one_flag\":64,\"new_subtypes_route_semantics_unresolved\":true}"
               << ",\"candidate_a0_tiles_with_success\":" << candidate_a0_tiles_with_success
               << ",\"tiles_with_incomplete_link_data\":" << tiles_with_incomplete_link_data
               << ",\"polygons_with_incomplete_link_data\":" << polygons_with_incomplete_link_data
               << ",\"shipping_poly_block_candidate_stride\":"
               << ShippingPolyRecordStride
               << ",\"shipping_poly_block_tiles_sampled\":"
               << shipping_poly_block_tiles_sampled
               << ",\"shipping_poly_block_prefixes_sampled\":"
               << shipping_poly_block_prefixes_sampled
               << ",\"shipping_poly_block_prefixes_valid\":"
               << shipping_poly_block_prefixes_valid
               << ",\"shipping_poly_block_prefixes_link_ref_plausible\":"
               << shipping_poly_block_prefixes_link_ref_plausible
               << ",\"shipping_poly_block_tiles_all_prefixes_valid\":"
               << shipping_poly_block_tiles_all_prefixes_valid
               << ",\"shipping_poly_block_tiles_all_link_refs_plausible\":"
               << shipping_poly_block_tiles_all_link_refs_plausible
               << ",\"shipping_poly_block_links_pointer_matches\":"
               << shipping_poly_block_links_pointer_matches
               << ",\"shipping_poly_block_links_pointer_mismatches\":"
               << shipping_poly_block_links_pointer_mismatches
               << ",\"shipping_poly_block_payload_bounds_valid\":"
               << shipping_poly_block_payload_bounds_valid
               << ",\"shipping_poly_block_payload_bounds_invalid\":"
               << shipping_poly_block_payload_bounds_invalid
               << ",\"candidate_validation\":{\"prefix_checks_passed\":"
               << (candidate_prefix_validation_passed ? "true" : "false")
               << ",\"dynamic_link_checks_passed\":"
               << (dynamic_link_candidate_validation_passed ? "true" : "false")
               << ",\"passed\":" << (candidate_validation_passed ? "true" : "false")
               << ",\"independently_validated\":"
               << (candidate_layout_independently_validated ? "true" : "false")
               << ",\"reason_code\":\""
               << (candidate_validation_passed
                   ? "independent_source_binary_proof_required"
                   : (!candidate_prefix_validation_passed
                       ? "shipping_block_prefix_or_payload_checks_failed"
                       : "dynamic_link_candidate_checks_failed"))
               << "\"}"
               << ",\"off_mesh_pointer_rejections\":" << off_mesh_pointer_rejections
               << ",\"off_mesh_read_failures\":" << off_mesh_read_failures
               << ",\"geometry_stage_skips\":" << geometry_stage_skips
               << ",\"rejection_samples\":[";
        for (std::size_t index = 0; index < rejection_samples.size(); ++index)
        {
            if (index != 0) output << ",";
            output << rejection_samples[index];
        }
        output << "],\"memory_diagnostic_samples\":[";
        for (std::size_t index = 0; index < memory_diagnostic_samples.size(); ++index)
        {
            if (index != 0) output << ",";
            output << memory_diagnostic_samples[index];
        }
        output << "],\"shipping_poly_block_diagnostic_samples\":[";
        for (std::size_t index = 0; index < shipping_poly_block_diagnostic_samples.size(); ++index)
        {
            if (index != 0)
            {
                output << ",";
            }
            output << shipping_poly_block_diagnostic_samples[index];
        }
        output << "],\"candidate_a0_link_diagnostic_samples\":[";
        for (std::size_t index = 0; index < candidate_a0_link_diagnostic_samples.size(); ++index)
        {
            if (index != 0)
            {
                output << ",";
            }
            output << candidate_a0_link_diagnostic_samples[index];
        }
        output << "],\"candidate_a0_link_diagnostic_samples_scope\":\"production_first_failure_only\""
               << ",\"diagnostic_dynamic_link_rejection_samples\":[";
        for (std::size_t index = 0; index < diagnostic_dynamic_link_rejection_samples.size(); ++index)
        {
            if (index != 0)
            {
                output << ",";
            }
            output << diagnostic_dynamic_link_rejection_samples[index];
        }
        output << "]"
               << ",\"truncated\":" << (truncated ? "true" : "false")
               << ",\"lookup_chain_complete\":"
               << (inventory.full_lookup_chain_traversed ? "true" : "false")
               << ",\"exported_tile_count_matches\":"
               << (exported_tile_count_matches ? "true" : "false")
               << ",\"topology_proof\":" << topology_proof
               << ",\"status\":\"" << (geometry_complete ? "complete" : "partial")
               << "\",\"reason_code\":\"" << geometry_reason
               << "\"}";
        return output.str();
    }

    struct NavGridRecord
    {
        std::wstring asset_name;
        std::int32_t grid_x{};
        std::int32_t grid_y{};
        std::int32_t layer_id{};
        Vec3 center{};
        float half_height{};
    };

    std::vector<NavGridRecord> ReadNavGridRecords(Unreal::UObject* actor)
    {
        std::vector<NavGridRecord> records;
        RawTArray grids{};
        if (actor == nullptr ||
            !ReadRawTArray(actor, 0x238, grids) ||
            grids.count < 0 || grids.count > 4096 ||
            (grids.count > 0 && !IsReadable(grids.data)))
        {
            return records;
        }
        records.reserve(static_cast<std::size_t>(grids.count));
        constexpr std::uintptr_t RecordStride = 0x24;
        for (std::int32_t index = 0; index < grids.count; ++index)
        {
            auto* record = static_cast<const std::uint8_t*>(grids.data) +
                static_cast<std::uintptr_t>(index) * RecordStride;
            Unreal::FName asset_name{};
            NavGridRecord value;
            if (!ReadValue(record, 0x00, asset_name) ||
                !ReadValue(record, 0x08, value.grid_x) ||
                !ReadValue(record, 0x0C, value.grid_y) ||
                !ReadValue(record, 0x10, value.layer_id) ||
                !ReadValue(record, 0x14, value.center) ||
                !ReadValue(record, 0x20, value.half_height) ||
                !std::isfinite(value.center.x) || !std::isfinite(value.center.y) ||
                !std::isfinite(value.center.z) || !std::isfinite(value.half_height))
            {
                continue;
            }
            try
            {
                value.asset_name = asset_name.ToString();
            }
            catch (const std::exception&)
            {
                value.asset_name = L"";
            }
            records.push_back(std::move(value));
        }
        return records;
    }

    bool ReadFunctionReturnInt(Unreal::UObject* context, Unreal::UFunction* function, std::int32_t& value)
    {
        if (context == nullptr || function == nullptr)
        {
            return false;
        }
        auto* return_property = function->GetReturnProperty();
        if (return_property == nullptr)
        {
            return false;
        }
        const auto offset = return_property->GetOffset_Internal();
        const auto size = std::max<std::size_t>(64, static_cast<std::size_t>(function->GetStructureSize()) + 16);
        if (offset < 0 || static_cast<std::size_t>(offset) + sizeof(value) > size)
        {
            return false;
        }
        std::vector<std::uint8_t> parameters(size, 0);
        context->ProcessEvent(function, parameters.data());
        std::memcpy(&value, parameters.data() + offset, sizeof(value));
        return true;
    }

    bool ReadFunctionReturnFloat(Unreal::UObject* context, Unreal::UFunction* function, float& value)
    {
        if (context == nullptr || function == nullptr)
        {
            return false;
        }
        auto* return_property = function->GetReturnProperty();
        if (return_property == nullptr)
        {
            return false;
        }
        const auto offset = return_property->GetOffset_Internal();
        const auto size = std::max<std::size_t>(64, static_cast<std::size_t>(function->GetStructureSize()) + 16);
        if (offset < 0 || static_cast<std::size_t>(offset) + sizeof(value) > size)
        {
            return false;
        }
        std::vector<std::uint8_t> parameters(size, 0);
        context->ProcessEvent(function, parameters.data());
        std::memcpy(&value, parameters.data() + offset, sizeof(value));
        return std::isfinite(value);
    }

    bool ReadSplinePoint(
        Unreal::UObject* context,
        Unreal::UFunction* function,
        std::int32_t point_index,
        Vec3& value)
    {
        if (context == nullptr || function == nullptr)
        {
            return false;
        }

        auto* return_property = function->GetReturnProperty();
        auto* first_property = function->GetFirstProperty();
        if (return_property == nullptr || first_property == nullptr)
        {
            return false;
        }

        Unreal::FProperty* index_property = nullptr;
        Unreal::FProperty* coordinate_space_property = nullptr;
        auto* property = first_property;
        for (std::int32_t property_index = 0; property != nullptr && property_index < 32; ++property_index)
        {
            const auto name = property->GetName();
            if (name == L"PointIndex" || name == L"Index")
            {
                index_property = property;
            }
            else if (name == L"CoordinateSpace")
            {
                coordinate_space_property = property;
            }
            if (!property->HasNext())
            {
                break;
            }
            property = property->GetNextFieldAsProperty();
        }
        if (index_property == nullptr || coordinate_space_property == nullptr)
        {
            return false;
        }

        const auto index_offset = index_property->GetOffset_Internal();
        const auto coordinate_offset = coordinate_space_property->GetOffset_Internal();
        const auto return_offset = return_property->GetOffset_Internal();
        const auto parameter_size = std::max<std::size_t>(
            64, static_cast<std::size_t>(function->GetStructureSize()) + 16);
        if (index_offset < 0 || coordinate_offset < 0 || return_offset < 0 ||
            static_cast<std::size_t>(index_offset) + sizeof(point_index) > parameter_size ||
            static_cast<std::size_t>(coordinate_offset) + sizeof(std::uint8_t) > parameter_size ||
            static_cast<std::size_t>(return_offset) + sizeof(value) > parameter_size)
        {
            return false;
        }

        std::vector<std::uint8_t> parameters(parameter_size, 0);
        std::memcpy(parameters.data() + index_offset, &point_index, sizeof(point_index));
        // ESplineCoordinateSpace::World is the second enum value in the
        // recovered UE4.26 header and is represented by TEnumAsByte.
        constexpr std::uint8_t WorldCoordinateSpace = 1;
        std::memcpy(parameters.data() + coordinate_offset, &WorldCoordinateSpace,
            sizeof(WorldCoordinateSpace));
        context->ProcessEvent(function, parameters.data());
        std::memcpy(&value, parameters.data() + return_offset, sizeof(value));
        return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
    }

    SplineProbe ProbeSpline(Unreal::UObject* component)
    {
        SplineProbe probe;
        if (component == nullptr || !IsReadable(component))
        {
            probe.status = "unavailable";
            probe.reason_code = "spline_component_unreadable";
            return probe;
        }

        auto* count_function = Unreal::UObjectGlobals::StaticFindObject<Unreal::UFunction*>(
            nullptr, nullptr, L"/Script/Engine.SplineComponent:GetNumberOfSplinePoints");
        auto* length_function = Unreal::UObjectGlobals::StaticFindObject<Unreal::UFunction*>(
            nullptr, nullptr, L"/Script/Engine.SplineComponent:GetSplineLength");
        auto* point_function = Unreal::UObjectGlobals::StaticFindObject<Unreal::UFunction*>(
            nullptr, nullptr, L"/Script/Engine.SplineComponent:GetLocationAtSplinePoint");
        if (count_function == nullptr || length_function == nullptr)
        {
            probe.status = "unavailable";
            probe.reason_code = "spline_reflected_function_unavailable";
            return probe;
        }

        try
        {
            if (!ReadFunctionReturnInt(component, count_function, probe.point_count) ||
                probe.point_count < 0 || probe.point_count > 4096)
            {
                probe.status = "invalid";
                probe.reason_code = "spline_point_count_invalid";
                return probe;
            }
            probe.length_valid = ReadFunctionReturnFloat(component, length_function, probe.length);
            if (point_function == nullptr)
            {
                probe.status = "metadata_only";
                probe.reason_code = "spline_point_function_unavailable";
                return probe;
            }
            probe.points.reserve(static_cast<std::size_t>(probe.point_count));
            for (std::int32_t point_index = 0; point_index < probe.point_count; ++point_index)
            {
                Vec3 point{};
                if (!ReadSplinePoint(component, point_function, point_index, point))
                {
                    probe.status = "partial";
                    probe.reason_code = "spline_point_read_failed";
                    return probe;
                }
                probe.points.push_back(point);
                ++probe.points_returned;
            }
            probe.point_list_complete = probe.points_returned == probe.point_count;
            probe.status = probe.point_list_complete ? "complete" : "partial";
            probe.reason_code = probe.point_list_complete
                ? "spline_points_complete"
                : "spline_point_count_mismatch";
        }
        catch (const std::exception&)
        {
            probe.status = "error";
            probe.reason_code = "spline_process_event_exception";
        }
        return probe;
    }

    std::string BuildAuthoredRouteInventory(Unreal::UObject* world_object)
    {
        std::ostringstream report;
        report << "{\"schema_version\":1,\"source\":\"ue4ss_native_authored_route_inventory\"";
        report << ",\"movement_sent\":false,\"world\":"
               << JsonString(world_object == nullptr ? L"" : world_object->GetFullName());
        if (world_object == nullptr || !IsReadable(world_object))
        {
            report << ",\"status\":\"world_unavailable\"}";
            return report.str();
        }

        void* persistent_level = nullptr;
        RawTArray loaded_levels{};
        if (!ReadValue(world_object, WorldPersistentLevelOffset, persistent_level) ||
            !IsReadable(persistent_level) ||
            !ReadRawTArray(world_object, WorldLoadedLevelsOffset, loaded_levels) ||
            loaded_levels.count > 256)
        {
            report << ",\"status\":\"level_inventory_unavailable\"}";
            return report.str();
        }

        std::vector<void*> levels;
        std::unordered_set<void*> seen_levels;
        if (seen_levels.insert(persistent_level).second)
        {
            levels.push_back(persistent_level);
        }
        for (std::int32_t index = 0; index < loaded_levels.count; ++index)
        {
            void* level = nullptr;
            if (!ReadValue(static_cast<const std::uint8_t*>(loaded_levels.data) + index * sizeof(void*), 0, level) ||
                !IsReadable(level) || !seen_levels.insert(level).second)
            {
                continue;
            }
            levels.push_back(level);
        }

        std::unordered_set<void*> seen_actors;
        std::int32_t actor_count = 0;
        std::int32_t level_actor_array_count = 0;
        std::int32_t level_actor_array_levels = 0;
        std::int32_t actor_cluster_count = 0;
        std::vector<Unreal::UObject*> route_actors;
        for (const auto level : levels)
        {
            RawTArray actors{};
            if (!ReadRawTArray(level, LevelActorsOffset, actors) || actors.count > 250000)
            {
                continue;
            }
            ++level_actor_array_levels;
            level_actor_array_count += actors.count;
            for (std::int32_t index = 0; index < actors.count; ++index)
            {
                void* actor_pointer = nullptr;
                if (!ReadValue(static_cast<const std::uint8_t*>(actors.data) + index * sizeof(void*), 0, actor_pointer) ||
                    !IsReadable(actor_pointer) || !seen_actors.insert(actor_pointer).second)
                {
                    continue;
                }
                auto* actor = reinterpret_cast<Unreal::UObject*>(actor_pointer);
                ++actor_count;
                void* actor_class_pointer = nullptr;
                if (!ReadValue(actor, UObjectClassOffset, actor_class_pointer) ||
                    !IsReadable(actor_class_pointer))
                {
                    continue;
                }
                const auto class_name = reinterpret_cast<Unreal::UObject*>(actor_class_pointer)->GetFullName();
                const bool is_route_candidate =
                    class_name.find(L"EndMobRouteSpline") != std::wstring::npos ||
                    class_name.find(L"EndPathWaySplineActor") != std::wstring::npos ||
                    class_name.find(L"EndNavGridActor") != std::wstring::npos ||
                    class_name.find(L"EndSplineActorBase") != std::wstring::npos ||
                    class_name.find(L"Route") != std::wstring::npos ||
                    class_name.find(L"PathWay") != std::wstring::npos ||
                    class_name.find(L"Patrol") != std::wstring::npos ||
                    class_name.find(L"Waypoint") != std::wstring::npos ||
                    class_name.find(L"WayPoint") != std::wstring::npos;
                if (!is_route_candidate || route_actors.size() >= 256)
                {
                    continue;
                }
                route_actors.push_back(actor);
            }

            // Keep the old cluster read as a comparison diagnostic. The
            // cluster contains only a small root set in this build; it is not
            // the authoritative level actor list.
            void* actor_cluster = nullptr;
            RawTArray cluster_actors{};
            if (ReadValue(level, LevelActorClusterOffset, actor_cluster) &&
                IsReadable(actor_cluster) &&
                ReadRawTArray(actor_cluster, ActorClusterActorsOffset, cluster_actors) &&
                cluster_actors.count >= 0 && cluster_actors.count <= 250000)
            {
                actor_cluster_count += cluster_actors.count;
            }
        }

        report << ",\"status\":\"ok\",\"level_count\":" << levels.size()
               << ",\"actor_count\":" << actor_count
               << ",\"level_actor_array_offset\":" << LevelActorsOffset
               << ",\"level_actor_array_levels\":" << level_actor_array_levels
               << ",\"level_actor_array_count\":" << level_actor_array_count
               << ",\"actor_cluster_count\":" << actor_cluster_count
               << ",\"route_actor_count\":" << route_actors.size()
               << ",\"actors\":[";
        for (std::size_t index = 0; index < route_actors.size(); ++index)
        {
            if (index != 0) report << ",";
            auto* actor = route_actors[index];
            void* actor_class_pointer = nullptr;
            ReadValue(actor, UObjectClassOffset, actor_class_pointer);
            const auto class_name = actor_class_pointer == nullptr
                ? L""
                : reinterpret_cast<Unreal::UObject*>(actor_class_pointer)->GetFullName();
            const auto actor_name = actor->GetFullName();
            const bool is_pathway_spline = class_name.find(L"EndPathWaySplineActor") != std::wstring::npos;
            const bool is_mob_route = class_name.find(L"EndMobRouteSpline") != std::wstring::npos;
            const bool is_nav_grid = class_name.find(L"EndNavGridActor") != std::wstring::npos;
            const auto component_offset = is_pathway_spline ? 0x240U : (is_mob_route ? 0x228U : 0U);
            void* component_pointer = nullptr;
            std::uint8_t is_loop = 0;
            if (component_offset != 0)
            {
                ReadValue(actor, component_offset, component_pointer);
            }
            if (is_mob_route)
            {
                ReadValue(actor, 0x230, is_loop);
            }
            std::vector<std::wstring> pathway_actor_names;
            if (is_pathway_spline)
            {
                RawTArray linked_actors{};
                if (ReadRawTArray(actor, 0x248, linked_actors) &&
                    linked_actors.count >= 0 && linked_actors.count <= 256)
                {
                    pathway_actor_names.reserve(static_cast<std::size_t>(linked_actors.count));
                    for (std::int32_t name_index = 0; name_index < linked_actors.count; ++name_index)
                    {
                        Unreal::FName name{};
                        if (!ReadValue(
                                static_cast<const std::uint8_t*>(linked_actors.data) +
                                    name_index * sizeof(Unreal::FName),
                                0,
                                name))
                        {
                            continue;
                        }
                        try
                        {
                            pathway_actor_names.push_back(name.ToString());
                        }
                        catch (const std::exception&)
                        {
                            // Preserve the actor record even if one name
                            // cannot be rendered by the UE4SS name bridge.
                        }
                    }
                }
            }
            auto* component = reinterpret_cast<Unreal::UObject*>(component_pointer);
            const auto spline = component == nullptr
                ? SplineProbe{"not_applicable", "actor_has_no_spline_component", -1, 0, false, 0.0f, false, {}}
                : ProbeSpline(component);
            const auto nav_grid_records = is_nav_grid ? ReadNavGridRecords(actor) : std::vector<NavGridRecord>{};
            report << "{\"actor\":" << JsonString(actor_name)
                   << ",\"class\":" << JsonString(class_name)
                   << ",\"component_offset\":" << component_offset
                   << ",\"is_loop\":" << (is_loop != 0 ? "true" : "false")
                   << ",\"component\":" << JsonString(component == nullptr ? L"" : component->GetFullName())
                   << ",\"spline_status\":\"" << spline.status
                   << "\",\"spline_reason_code\":\"" << spline.reason_code
                   << "\",\"point_count\":" << spline.point_count
                   << ",\"points_returned\":" << spline.points_returned
                   << ",\"point_list_complete\":" << (spline.point_list_complete ? "true" : "false")
                   << ",\"length\":" << spline.length
                   << ",\"length_valid\":" << (spline.length_valid ? "true" : "false")
                   << ",\"nav_grids\":[";
            for (std::size_t grid_index = 0; grid_index < nav_grid_records.size(); ++grid_index)
            {
                if (grid_index != 0) report << ",";
                const auto& grid = nav_grid_records[grid_index];
                report << "{\"asset_name\":" << JsonString(grid.asset_name)
                       << ",\"grid_x\":" << grid.grid_x
                       << ",\"grid_y\":" << grid.grid_y
                       << ",\"layer_id\":" << grid.layer_id
                       << ",\"center\":" << JsonVec(grid.center)
                       << ",\"half_height\":" << grid.half_height << "}";
            }
            report << "]"
                   << ",\"path_way_actor_names\":[";
            for (std::size_t name_index = 0; name_index < pathway_actor_names.size(); ++name_index)
            {
                if (name_index != 0) report << ",";
                report << JsonString(pathway_actor_names[name_index]);
            }
            report << "]"
                   << ",\"points\":[";
            for (std::size_t point_index = 0; point_index < spline.points.size(); ++point_index)
            {
                if (point_index != 0) report << ",";
                report << JsonVec(spline.points[point_index]);
            }
            report << "]}";
        }
        report << "]}";
        return report.str();
    }

    PathQueryOutput RunNativePathQuery(void* main_nav_data, void* find_path, void* default_filter_shared_pointer_address, Unreal::UObject* owner)
    {
        PathQueryOutput output;
        Target target;
        Vec3 start;
        if (!ReadTarget(target))
        {
            output.json = R"({"status":"skipped","reason_code":"target_unavailable"})";
            return output;
        }
        if (!target.has_start_position && !ReadPlayerPosition(start))
        {
            output.json = R"({"status":"skipped","reason_code":"player_position_unavailable"})";
            return output;
        }
        if (target.has_start_position)
        {
            start = target.start_position;
        }
        if (!IsExecutable(find_path))
        {
            output.json = R"({"status":"skipped","reason_code":"find_path_pointer_not_executable"})";
            return output;
        }
        if (!IsReadable(default_filter_shared_pointer_address, sizeof(RawSharedPtr)))
        {
            output.json = R"({"status":"skipped","reason_code":"default_filter_shared_pointer_unreadable"})";
            return output;
        }

        RawSharedPtr default_filter{};
        std::memcpy(&default_filter, default_filter_shared_pointer_address, sizeof(default_filter));
        if (default_filter.object == nullptr || !IsReadable(default_filter.object))
        {
            output.json = R"({"status":"skipped","reason_code":"default_filter_object_unavailable"})";
            return output;
        }

        RawNavAgentProperties agent;
        agent.agent_radius = 35.0f;
        agent.agent_height = 210.0f;
        agent.agent_step_height = -1.0f;
        agent.nav_walking_search_height_scale = 0.5f;
        agent.agent_slope = -1.0f;

        RawPathFindingQuery query{};
        query.owner = owner;
        query.start_location = start;
        query.end_location = target.position;
        query.query_filter = default_filter;
        query.cost_limit = (std::numeric_limits<float>::max)();
        query.nav_data_flags = 0;
        query.allow_partial_paths = 0;
        query.nav_data = reinterpret_cast<Unreal::UObject*>(main_nav_data);
        query.nav_agent_properties = agent;

        void* filter_implementation = nullptr;
        void* filter_reference_controller = nullptr;
        std::uint32_t filter_max_search_nodes = 0;
        ReadValue(default_filter.object, 0x00, filter_implementation);
        ReadValue(default_filter.object, 0x08, filter_reference_controller);
        ReadValue(default_filter.object, 0x20, filter_max_search_nodes);
        const bool query_owner_valid = owner != nullptr && query.owner.Get() == owner;
        const bool query_nav_data_valid = query.nav_data.Get() == reinterpret_cast<Unreal::UObject*>(main_nav_data);

        using FindPathFunction = RawPathFindingResult (*)(const RawNavAgentProperties&, const RawPathFindingQuery&);
        const auto function = reinterpret_cast<FindPathFunction>(find_path);
        const auto begin = std::chrono::steady_clock::now();
        // This is the only call in the module that can execute native path
        // finding, and it is reachable only through the explicit marker file.
        RawPathFindingResult result = function(agent, query);
        const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - begin).count();
        output.called = true;

        std::ostringstream json;
        constexpr std::uint8_t NavigationQuerySuccess = 3;
        json << "{\"status\":\"" << (result.result == NavigationQuerySuccess ? "success" : "failure")
             << "\",\"result_code\":" << static_cast<unsigned int>(result.result)
             << ",\"target_id\":\"" << Escape(std::wstring(target.id.begin(), target.id.end()))
             << "\",\"start\":" << JsonVec(start)
             << ",\"target\":" << JsonVec(target.position)
             << ",\"filter_object\":\"" << Hex(default_filter.object)
             << "\",\"filter_reference_controller\":\"" << Hex(filter_reference_controller)
             << "\",\"filter_implementation\":\"" << Hex(filter_implementation)
             << "\",\"filter_max_search_nodes\":" << filter_max_search_nodes
             << ",\"query_owner_valid\":" << (query_owner_valid ? "true" : "false")
             << ",\"query_nav_data_valid\":" << (query_nav_data_valid ? "true" : "false")
             << ",\"query_duration_us\":" << elapsed;

        auto* path_object = result.path.object;
        if (path_object != nullptr)
        {
            json << ",\"path_object\":\"" << Hex(path_object) << "\"";
        }
        if (result.result == NavigationQuerySuccess)
        {
            // FNavigationPath::PathPoints is protected and its shipping
            // layout is not present in the UE4SS dump. Do not guess an
            // offset here: a successful query is diagnostic only until that
            // layout is independently recovered.
            json << ",\"point_count\":0,\"reason_code\":\"path_points_layout_unverified\"";
        }
        else
        {
            json << ",\"point_count\":0,\"reason_code\":\"path_points_unavailable_or_incomplete\"";
        }
        json << "}";
        output.json = json.str();
        return output;
    }

    bool WriteJsonFile(const wchar_t* output_path, const std::string& body)
    {
        const std::filesystem::path output(output_path);
        const auto temporary = output.wstring() + L".tmp";
        {
            std::ofstream file(temporary, std::ios::binary | std::ios::trunc);
            if (!file) return false;
            file << body;
            if (!file.good()) return false;
        }
        // The harness polls these files with Python's open(), which does not
        // share delete access, so a replace during a read fails with a
        // sharing violation or access denied. Live 2026-10-01 at Gongaga a
        // single failed rename stranded {"state":"completed"} in the .tmp
        // and the capture timed out. Retry for about a second; this runs on
        // the one-shot capture path in on_update, not the game thread.
        for (int attempt = 0; attempt < MoveFileRetryAttempts; ++attempt)
        {
            if (MoveFileExW(temporary.c_str(), output.c_str(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0)
            {
                return true;
            }
            const DWORD error = GetLastError();
            if ((error != ERROR_SHARING_VIOLATION && error != ERROR_ACCESS_DENIED)
                || attempt + 1 == MoveFileRetryAttempts)
            {
                break;
            }
            Sleep(MoveFileRetrySleepMs);
        }
        // Keep the .tmp: the harness accepts a terminal status stranded
        // there for its own request, and the next write truncates it.
        return false;
    }

    bool WriteReport(const std::string& body)
    {
        return WriteJsonFile(OutputPath, body);
    }

    std::string AddCaptureRequestMetadata(const std::string& body)
    {
        if (!ActiveCaptureRequest.valid || body.empty() || body.front() != '{')
        {
            return body;
        }
        std::ostringstream output;
        output << "{\"request_id\":\"" << Escape(ActiveCaptureRequest.request_id)
               << "\",\"capture_session_id\":\"" << Escape(ActiveCaptureRequest.session_id)
               << "\",\"capture_world_id\":\"" << Escape(ActiveCaptureRequest.world_id)
               << "\",\"capture_map_id\":\"" << Escape(ActiveCaptureRequest.map_id)
               << "\",\"native_probe_sha256\":\"" << Escape(ActiveCaptureRequest.native_probe_sha256)
               << "\",\"capture_purpose\":\"" << Escape(ActiveCaptureRequest.purpose)
               << "\"," << body.substr(1);
        return output.str();
    }

    bool WriteCaptureStatus(const char* state, const char* error = nullptr, const char* artifact_status = nullptr)
    {
        if (!ActiveCaptureRequest.valid)
        {
            return false;
        }
        long long elapsed_us = 0;
        if (ActiveCaptureStatusWritten)
        {
            elapsed_us = std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - ActiveCaptureStarted).count();
        }
        std::ostringstream output;
        output << "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_capture\","
               << "\"request_id\":\"" << Escape(ActiveCaptureRequest.request_id) << "\","
               << "\"state\":\"" << Escape(state) << "\","
               << "\"timestamp_utc\":\"" << UtcNow() << "\","
               << "\"elapsed_us\":" << elapsed_us << ","
               << "\"session_id\":\"" << Escape(ActiveCaptureRequest.session_id) << "\","
               << "\"world_id\":\"" << Escape(ActiveCaptureRequest.world_id) << "\","
               << "\"map_id\":\"" << Escape(ActiveCaptureRequest.map_id) << "\","
               << "\"purpose\":\"" << Escape(ActiveCaptureRequest.purpose) << "\"";
        if (error != nullptr)
        {
            output << ",\"error\":\"" << Escape(error) << "\"";
        }
        if (artifact_status != nullptr)
        {
            output << ",\"artifact_status\":\"" << Escape(artifact_status) << "\"";
        }
        output << "}";
        const bool written = WriteJsonFile(GeometryStatusPath, output.str());
        ActiveCaptureStatusWritten = true;
        return written;
    }

    bool WriteTimingReport(const ProbeTiming& timing)
    {
        std::ostringstream output;
        output << "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_probe_timing\","
               << "\"request_id\":\"" << Escape(timing.request_id) << "\","
               << "\"full_build_report_us\":" << timing.full_build_report_us << ","
               << "\"geometry_extraction_and_json_serialization_us\":"
               << timing.geometry_extraction_and_json_serialization_us << ","
               << "\"geometry_json_write_us\":" << timing.geometry_json_write_us << ","
               << "\"probe_report_json_serialization_us\":"
               << timing.probe_report_json_serialization_us << ","
               << "\"probe_json_write_us\":" << timing.probe_json_write_us << ","
               << "\"probe_attempt\":" << timing.probe_attempt << ","
               << "\"geometry_armed\":" << (timing.geometry_armed ? "true" : "false") << ","
               << "\"geometry_dump_written\":" << (timing.geometry_dump_written ? "true" : "false")
               << ",\"movement_sent\":false}";
        return WriteJsonFile(TimingOutputPath, AddCaptureRequestMetadata(output.str()));
    }

    void WriteAuthoredRouteReport(const std::string& body)
    {
        WriteJsonFile(AuthoredRouteOutputPath, body);
    }

    std::string BuildReport()
    {
        // Validate the host executable before touching any shipping-build
        // object offsets. The probe is deliberately build-bound: even its
        // read-only diagnostics must not dereference private layouts from a
        // future executable through stale offsets.
        const auto executable_identity = GetExecutableIdentity();
        const bool native_query_requested = std::filesystem::exists(EnablePath);
        const bool geometry_marker_present = std::filesystem::exists(GeometryEnablePath);
        const bool geometry_readiness_present = std::filesystem::exists(GeometryReadyPath);
        const bool geometry_requested = geometry_marker_present && geometry_readiness_present;
        if (!executable_identity.valid())
        {
            const bool native_query_marker_consumed = native_query_requested &&
                ConsumeOneShotMarker(EnablePath);
            const bool geometry_marker_consumed = geometry_requested &&
                ConsumeOneShotMarker(GeometryEnablePath);
            std::ostringstream skipped;
            skipped << "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_probe\","
                    << "\"status\":\"skipped\",\"reason_code\":\"known_executable_build_mismatch\","
                    << "\"native_path_query_requested\":" << (native_query_requested ? "true" : "false") << ","
                    << "\"native_path_query_marker_consumed\":" << (native_query_marker_consumed ? "true" : "false") << ","
                    << "\"native_geometry_marker_present\":" << (geometry_marker_present ? "true" : "false") << ","
                    << "\"native_geometry_readiness_present\":" << (geometry_readiness_present ? "true" : "false") << ","
                    << "\"native_geometry_requested\":" << (geometry_requested ? "true" : "false") << ","
                    << "\"native_geometry_marker_consumed\":" << (geometry_marker_consumed ? "true" : "false") << ","
                    << "\"native_path_query_called\":false,\"native_geometry_dump_written\":false,"
                    << "\"executable_identity\":{\"path\":\"" << Escape(executable_identity.path)
                    << "\",\"sha256\":\"" << executable_identity.sha256
                    << "\",\"known_sha256\":\"" << KnownGameSha256
                    << "\",\"filename_matches\":" << (executable_identity.filename_matches ? "true" : "false")
                    << ",\"sha256_matches\":" << (executable_identity.sha256_matches ? "true" : "false")
                    << ",\"valid\":false},\"movement_sent\":false}";
            return skipped.str();
        }

        Unreal::UObject* world_object = nullptr;
        std::wstring selected_world_name;
        auto consider_world = [&](void* candidate_pointer)
        {
            if (candidate_pointer == nullptr || !IsReadable(candidate_pointer))
            {
                return;
            }
            auto* candidate = reinterpret_cast<Unreal::UObject*>(candidate_pointer);
            const auto candidate_name = candidate->GetFullName();
            const bool candidate_is_game_world = candidate_name.find(L"/Game/Level/Game/") != std::wstring::npos;
            const bool selected_is_transient = selected_world_name.find(L"/Temp/") != std::wstring::npos;
            if (world_object == nullptr || (selected_is_transient && candidate_is_game_world))
            {
                world_object = candidate;
                selected_world_name = candidate_name;
            }
        };

        auto consider_viewport = [&](void* viewport_pointer)
        {
            void* candidate_world = nullptr;
            if (ReadValue(viewport_pointer, GameViewportWorldOffset, candidate_world))
            {
                consider_world(candidate_world);
            }
        };

        // Prefer the live viewport path. The verified UE4.26 CXX dump places
        // UEngine::GameViewport at 0x7E0 and UGameViewportClient::World at
        // 0x80. This avoids FindAllOf/actor enumeration, which has crashed in
        // this shipping build, and avoids treating a PlayerController CDO as
        // the active gameplay context.
        if (auto* engine = Unreal::UObjectGlobals::FindFirstOf(L"Engine"); engine != nullptr)
        {
            void* viewport = nullptr;
            if (ReadValue(engine, EngineGameViewportOffset, viewport))
            {
                consider_viewport(viewport);
            }
        }

        if (auto* viewport = Unreal::UObjectGlobals::FindFirstOf(L"GameViewportClient"); viewport != nullptr)
        {
            consider_viewport(viewport);
        }

        // The Lua inventory probe has established that a narrowly scoped
        // PlayerController FindAllOf is safe in this build, unlike the broad
        // EndCharacter scans that previously crashed. Use the controller's
        // verified CXX Pawn field only to obtain its world; do not enumerate
        // or inspect any other actor state.
        std::vector<Unreal::UObject*> player_controllers;
        Unreal::UObject* player_pawn_object = nullptr;
        Unreal::UObjectGlobals::FindAllOf(L"PlayerController", player_controllers);
        for (auto* controller : player_controllers)
        {
            if (controller == nullptr || !IsReadable(controller))
            {
                continue;
            }
            void* pawn = nullptr;
            if (!ReadValue(controller, ControllerPawnOffset, pawn) || !IsReadable(pawn))
            {
                continue;
            }
            if (player_pawn_object == nullptr)
            {
                player_pawn_object = reinterpret_cast<Unreal::UObject*>(pawn);
            }
            consider_world(reinterpret_cast<void*>(reinterpret_cast<Unreal::UObject*>(pawn)->GetWorld()));
            if (world_object != nullptr && selected_world_name.find(L"/Game/Level/Game/") != std::wstring::npos)
            {
                break;
            }
        }

        // Keep the direct object-context fallback for builds where the
        // viewport singleton is not discoverable, but never replace an
        // already selected game world with a transient world.
        const wchar_t* context_classes[] = {L"PlayerController", L"Pawn", L"Character", L"World"};
        for (const auto* class_name : context_classes)
        {
            auto* context_object = Unreal::UObjectGlobals::FindFirstOf(class_name);
            if (context_object == nullptr)
            {
                continue;
            }
            consider_world(reinterpret_cast<void*>(context_object->GetWorld()));
        }
        if (world_object == nullptr)
        {
            return R"({"schema_version":1,"source":"ue4ss_native_navigation_probe","status":"world_unavailable","world_lookup":"Engine.GameViewport.World_then_GameViewportClient.World_then_context_fallback","movement_sent":false,"native_path_query_called":false})";
        }

        void* navigation_system = nullptr;
        if (!ReadValue(world_object, WorldNavigationSystemOffset, navigation_system) || !IsReadable(navigation_system))
        {
            return "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_probe\",\"status\":\"navigation_system_unavailable\",\"world\":\"" + Escape(world_object->GetFullName()) + "\",\"movement_sent\":false,\"native_path_query_called\":false}";
        }

        void* main_nav_data = nullptr;
        if (!ReadValue(navigation_system, NavigationMainNavDataOffset, main_nav_data) || !IsReadable(main_nav_data))
        {
            return "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_probe\",\"status\":\"main_nav_data_unavailable\",\"navigation_system\":\"" + Hex(navigation_system) + "\",\"movement_sent\":false,\"native_path_query_called\":false}";
        }

        // A title/menu world can already contain EndNavigationSystem and
        // MainNavData. Do not mistake that resident background world for a
        // playable route snapshot: wait until the live UE4SS state exporter
        // has published Cloud's spatial pose before reading or interpreting
        // private Recast/Detour state. Keeping the marker files untouched
        // here lets the next bounded probe attempt perform the diagnostic
        // after Continue has actually entered gameplay.
        Vec3 live_player_position{};
        const bool gameplay_pose_available = ReadPlayerPosition(live_player_position);
        if (!gameplay_pose_available)
        {
            std::ostringstream waiting;
            waiting << "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_probe\","
                    << "\"status\":\"waiting_for_gameplay\","
                    << "\"reason_code\":\"player_pose_unavailable\","
                    << "\"world\":\"" << Escape(world_object->GetFullName()) << "\","
                    << "\"navigation_system\":\"" << Hex(navigation_system) << "\","
                    << "\"main_nav_data\":\"" << Hex(main_nav_data) << "\","
                    << "\"native_path_query_requested\":" << (native_query_requested ? "true" : "false") << ","
                    << "\"native_geometry_requested\":" << (geometry_requested ? "true" : "false") << ","
                    << "\"movement_sent\":false}";
            return waiting.str();
        }

        void* find_path = nullptr;
        void* default_filter_object = nullptr;
        ReadValue(main_nav_data, NavigationDataFindPathOffset, find_path);
        ReadValue(main_nav_data, NavigationDataDefaultFilterOffset, default_filter_object);

        std::uint32_t recast_nav_mesh_version = 0;
        void* recast_impl = nullptr;
        void* recast_impl_owner = nullptr;
        void* detour_nav_mesh = nullptr;
        void* default_filter_reference_controller = nullptr;
        void* default_filter_impl = nullptr;
        std::uint32_t default_filter_max_search_nodes = 0;
        ReadValue(default_filter_object, 0x08, default_filter_reference_controller);
        ReadValue(default_filter_object, 0x10, default_filter_impl);
        ReadValue(default_filter_object, 0x20, default_filter_max_search_nodes);
        ReadValue(main_nav_data, RecastNavMeshVersionOffset, recast_nav_mesh_version);
        ReadValue(main_nav_data, RecastNavMeshImplOffset, recast_impl);
        std::int32_t recast_tile_pool_size = 0;
        float recast_tile_size = 0.0f;
        float recast_cell_size = 0.0f;
        float recast_cell_height = 0.0f;
        float recast_agent_radius = 0.0f;
        float recast_agent_height = 0.0f;
        float recast_agent_slope = 0.0f;
        ReadValue(main_nav_data, 0x478, recast_tile_pool_size);
        ReadValue(main_nav_data, 0x47C, recast_tile_size);
        ReadValue(main_nav_data, 0x480, recast_cell_size);
        ReadValue(main_nav_data, 0x484, recast_cell_height);
        ReadValue(main_nav_data, 0x488, recast_agent_radius);
        ReadValue(main_nav_data, 0x48C, recast_agent_height);
        ReadValue(main_nav_data, 0x490, recast_agent_slope);
        if (IsReadable(recast_impl))
        {
            ReadValue(recast_impl, RecastNavMeshImplOwnerOffset, recast_impl_owner);
            ReadValue(recast_impl, RecastNavMeshImplDetourOffset, detour_nav_mesh);
        }

        std::vector<std::uintptr_t> recast_impl_words;
        if (IsReadable(recast_impl, 0x80))
        {
            recast_impl_words.reserve(16);
            for (std::uintptr_t offset = 0; offset < 0x80; offset += sizeof(std::uintptr_t))
            {
                std::uintptr_t word = 0;
                ReadValue(recast_impl, offset, word);
                recast_impl_words.push_back(word);
            }
        }

        std::int32_t detour_params_max_tiles = 0;
        std::int32_t detour_params_max_polys = 0;
        std::int32_t detour_max_tiles = 0;
        std::int32_t detour_max_polys = 0;
        float detour_params_tile_width = 0.0f;
        float detour_params_tile_height = 0.0f;
        std::int32_t detour_tile_lut_size = 0;
        std::int32_t detour_tile_lut_mask = 0;
        void* detour_pos_lookup = nullptr;
        void* detour_next_free = nullptr;
        void* detour_tiles = nullptr;
        std::uint32_t detour_salt_bits = 0;
        std::uint32_t detour_tile_bits = 0;
        std::uint32_t detour_poly_bits = 0;
        if (IsReadable(detour_nav_mesh, DetourNavMeshPolyBitsOffset + sizeof(std::uint32_t)))
        {
            ReadValue(detour_nav_mesh, 0x0C, detour_params_tile_width);
            ReadValue(detour_nav_mesh, 0x10, detour_params_tile_height);
            ReadValue(detour_nav_mesh, 0x14, detour_params_max_tiles);
            ReadValue(detour_nav_mesh, 0x18, detour_params_max_polys);
            ReadValue(detour_nav_mesh, 0x30, detour_max_tiles);
            ReadValue(detour_nav_mesh, 0x34, detour_tile_lut_size);
            ReadValue(detour_nav_mesh, 0x38, detour_tile_lut_mask);
            ReadValue(detour_nav_mesh, 0x80, detour_pos_lookup);
            ReadValue(detour_nav_mesh, 0x88, detour_next_free);
            ReadValue(detour_nav_mesh, 0x90, detour_tiles);
            ReadValue(detour_nav_mesh, DetourNavMeshTileBitsOffset, detour_tile_bits);
            ReadValue(detour_nav_mesh, DetourNavMeshPolyBitsOffset, detour_poly_bits);
            detour_salt_bits = (detour_tile_bits + detour_poly_bits < 64)
                ? 64 - detour_tile_bits - detour_poly_bits : 0;
            // dtNavMesh stores maxPolys only in the serialized params; the
            // runtime object has m_maxTiles but no separate m_maxPolys field.
            detour_max_polys = detour_params_max_polys;
        }
        struct TileStrideDiagnostic
        {
            std::uintptr_t stride{};
            std::int32_t valid_tiles{};
            std::int32_t first_valid_index{-1};
            std::int32_t first_poly_count{};
            std::int32_t first_vert_count{};
        };
        std::vector<TileStrideDiagnostic> tile_stride_diagnostics;
        // The recovered UE4.26 Detour source/layout gives a base dtMeshTile
        // stride of 0x68 before optional segment/cluster-link extensions;
        // those extensions produce larger variants. Keep the source-derived
        // 0x68/0x70/0x78 candidates alongside the previously observed
        // 0x80+ variants, and scan the complete bounded pool rather than
        // silently skipping Rebirth's 12,800-slot allocation.
        const std::uintptr_t tile_stride_candidates[] = {
            0x68, 0x70, 0x78, 0x80, 0x88, 0x98, 0xA0, 0xA8, 0xB0, 0xB8, 0xC0, 0xC8, 0xD0, 0xD8, 0xE0, 0xE8, 0xF0
        };
        const std::int32_t tile_scan_limit = (detour_max_tiles > 0 && detour_max_tiles <= 20000) ? detour_max_tiles : 0;
        if (IsReadable(detour_tiles, 0x20) && tile_scan_limit > 0)
        {
            for (const auto stride : tile_stride_candidates)
            {
                TileStrideDiagnostic diagnostic;
                diagnostic.stride = stride;
                for (std::int32_t index = 0; index < tile_scan_limit; ++index)
                {
                    auto* tile = static_cast<std::uint8_t*>(detour_tiles) + static_cast<std::uintptr_t>(index) * stride;
                    void* header = nullptr;
                    std::uint32_t magic = 0;
                    std::int32_t version = 0;
                    if (!ReadValue(tile, 0x08, header) || !ReadValue(header, 0x00, magic) ||
                        !ReadValue(header, 0x04, version) || magic != DetourNavMeshMagic ||
                        (version != DetourNavMeshVersion && version != RebirthDetourNavMeshVersion))
                    {
                        continue;
                    }
                    ++diagnostic.valid_tiles;
                    if (diagnostic.first_valid_index < 0)
                    {
                        diagnostic.first_valid_index = index;
                        ReadValue(header, 0x18, diagnostic.first_poly_count);
                        ReadValue(header, 0x1C, diagnostic.first_vert_count);
                    }
                }
                tile_stride_diagnostics.push_back(diagnostic);
            }
        }

        // The reflected class size excludes private non-UPROPERTY members in
        // some shipping layouts. Search only the narrow post-property region
        // for a candidate whose first FPImpl field points back to this nav
        // actor. This is a read-only layout diagnostic, never a method call.
        std::uintptr_t scanned_impl_offset = 0;
        void* scanned_impl = nullptr;
        for (std::uintptr_t offset = 0x4E8; offset <= 0x600; offset += sizeof(std::uintptr_t))
        {
            void* candidate = nullptr;
            void* candidate_owner = nullptr;
            if (!ReadValue(main_nav_data, offset, candidate) || !IsReadable(candidate, 0x10) ||
                !ReadValue(candidate, 0x00, candidate_owner) || candidate_owner != main_nav_data)
            {
                continue;
            }
            scanned_impl_offset = offset;
            scanned_impl = candidate;
            break;
        }
        if (scanned_impl != nullptr)
        {
            // Prefer the candidate proven to point back to this nav actor.
            // The reflected property boundary is not the private native
            // layout boundary in this shipping build.
            recast_impl = scanned_impl;
            ReadValue(recast_impl, RecastNavMeshImplOwnerOffset, recast_impl_owner);
            ReadValue(recast_impl, RecastNavMeshImplDetourOffset, detour_nav_mesh);
            recast_impl_words.clear();
            if (IsReadable(recast_impl, 0x80))
            {
                recast_impl_words.reserve(16);
                for (std::uintptr_t offset = 0; offset < 0x80; offset += sizeof(std::uintptr_t))
                {
                    std::uintptr_t word = 0;
                    ReadValue(recast_impl, offset, word);
                    recast_impl_words.push_back(word);
                }
            }
        }

        // The fixed offset above is only a candidate. If the owner-validated
        // scan found a different implementation, every Detour field must be
        // read again from that corrected object before it is reported. The
        // first implementation of this probe failed to do that and could
        // therefore report the provisional object's zero tile state.
        if (scanned_impl != nullptr)
        {
            detour_params_max_tiles = 0;
            detour_params_max_polys = 0;
            detour_max_tiles = 0;
            detour_max_polys = 0;
            detour_params_tile_width = 0.0f;
            detour_params_tile_height = 0.0f;
            detour_tile_lut_size = 0;
            detour_tile_lut_mask = 0;
            detour_pos_lookup = nullptr;
            detour_next_free = nullptr;
            detour_tiles = nullptr;
            detour_salt_bits = 0;
            detour_tile_bits = 0;
            detour_poly_bits = 0;
            tile_stride_diagnostics.clear();

            if (IsReadable(detour_nav_mesh, DetourNavMeshPolyBitsOffset + sizeof(std::uint32_t)))
            {
                ReadValue(detour_nav_mesh, 0x0C, detour_params_tile_width);
                ReadValue(detour_nav_mesh, 0x10, detour_params_tile_height);
                ReadValue(detour_nav_mesh, 0x14, detour_params_max_tiles);
                ReadValue(detour_nav_mesh, 0x18, detour_params_max_polys);
                ReadValue(detour_nav_mesh, 0x30, detour_max_tiles);
                ReadValue(detour_nav_mesh, 0x34, detour_tile_lut_size);
                ReadValue(detour_nav_mesh, 0x38, detour_tile_lut_mask);
                ReadValue(detour_nav_mesh, 0x80, detour_pos_lookup);
                ReadValue(detour_nav_mesh, 0x88, detour_next_free);
                ReadValue(detour_nav_mesh, 0x90, detour_tiles);
                ReadValue(detour_nav_mesh, DetourNavMeshTileBitsOffset, detour_tile_bits);
                ReadValue(detour_nav_mesh, DetourNavMeshPolyBitsOffset, detour_poly_bits);
                detour_salt_bits = (detour_tile_bits + detour_poly_bits < 64)
                    ? 64 - detour_tile_bits - detour_poly_bits : 0;
                detour_max_polys = detour_params_max_polys;
            }

            if (IsReadable(detour_tiles, 0x20) && detour_max_tiles > 0 && detour_max_tiles <= 20000)
            {
                for (const auto stride : tile_stride_candidates)
                {
                    TileStrideDiagnostic diagnostic;
                    diagnostic.stride = stride;
                    for (std::int32_t index = 0; index < detour_max_tiles; ++index)
                    {
                        auto* tile = static_cast<std::uint8_t*>(detour_tiles) +
                            static_cast<std::uintptr_t>(index) * stride;
                        void* header = nullptr;
                        std::uint32_t magic = 0;
                        std::int32_t version = 0;
                        if (!ReadValue(tile, 0x08, header) || !ReadValue(header, 0x00, magic) ||
                            !ReadValue(header, 0x04, version) || magic != DetourNavMeshMagic ||
                            (version != DetourNavMeshVersion && version != RebirthDetourNavMeshVersion))
                        {
                            continue;
                        }
                        ++diagnostic.valid_tiles;
                        if (diagnostic.first_valid_index < 0)
                        {
                            diagnostic.first_valid_index = index;
                            ReadValue(header, 0x18, diagnostic.first_poly_count);
                            ReadValue(header, 0x1C, diagnostic.first_vert_count);
                        }
                    }
                    tile_stride_diagnostics.push_back(diagnostic);
                }
            }
        }

        // Run the pointer-table inventory only after owner validation and the
        // corrected Detour fields have been re-read. Unlike the contiguous
        // pool scan above, this uses dtNavMesh's source-defined m_posLookup
        // table of active dtMeshTile pointers and does not depend on the
        // optional UE4 tile-struct extensions or their stride.
        // Remember readable regions through the tile scan and geometry export.
        ReadableRegionCache region_cache;
        const auto tile_lookup_inventory = ScanTileLookup(detour_pos_lookup, detour_tile_lut_size);

        bool geometry_marker_consumed = false;
        bool geometry_dump_written = false;
        if (geometry_requested)
        {
            std::string geometry_report;
            // A world and navigation object can exist during the menu/loading
            // boundary before any active tile has been installed. Keep the
            // capability marker intact in that state so a pre-start marker
            // cannot consume itself into an empty authoritative-looking
            // report. The next bounded report will retry once tile inventory
            // is non-empty.
            if (tile_lookup_inventory.unique_active_tiles == 0)
            {
                geometry_report =
                    R"({"status":"waiting","reason_code":"navmesh_tile_inventory_empty"})";
            }
            else
            {
                // Geometry export is a separate one-shot capability grant.
                // Consume it only immediately before the diagnostic export,
                // after active tile inventory exists.
                geometry_marker_consumed = ConsumeOneShotMarker(GeometryEnablePath);
                if (!geometry_marker_consumed)
                {
                    geometry_report = R"({"status":"skipped","reason_code":"geometry_marker_not_consumed"})";
                }
                else if (!executable_identity.valid())
                {
                    std::ostringstream skipped;
                    skipped << "{\"status\":\"skipped\",\"reason_code\":\"known_executable_build_mismatch\","
                            << "\"filename_matches\":" << (executable_identity.filename_matches ? "true" : "false")
                            << ",\"sha256_matches\":" << (executable_identity.sha256_matches ? "true" : "false") << "}";
                    geometry_report = skipped.str();
                }
                else
                {
                    WriteCaptureStatus("geometry_extracting");
                    const auto geometry_started = std::chrono::steady_clock::now();
                    geometry_report = BuildTileGeometryDump(tile_lookup_inventory,
                        detour_tiles, detour_max_tiles, detour_tile_bits, detour_poly_bits,
                        gameplay_pose_available ? &live_player_position : nullptr,
                        LastProbeTiming.geometry_export_complete);
                    WriteCaptureStatus("geometry_serializing");
                    LastProbeTiming.geometry_extraction_and_json_serialization_us =
                        std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - geometry_started).count();
                    geometry_dump_written = true;
                }
            }
            const auto geometry_write_started = std::chrono::steady_clock::now();
            const bool geometry_file_written = WriteJsonFile(
                GeometryOutputPath, AddCaptureRequestMetadata(geometry_report));
            geometry_dump_written = geometry_dump_written && geometry_file_written;
            LastProbeTiming.geometry_json_write_us =
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - geometry_write_started).count();
            if (geometry_file_written && geometry_dump_written)
            {
                WriteCaptureStatus("geometry_written");
            }
        }
        region_cache.End();

        const std::uintptr_t filter_scan_offsets[] = {0x350, 0x358, 0x360, 0x368, 0x370, 0x378, 0x380, 0x388, 0x390};

        std::ostringstream report;
        report << "{\"schema_version\":1,\"source\":\"ue4ss_native_navigation_probe\","
               << "\"status\":\"navigation_native_objects_available\","
               << "\"world\":\"" << Escape(world_object->GetFullName()) << "\","
               << "\"navigation_system\":\"" << Hex(navigation_system) << "\","
               << "\"main_nav_data\":\"" << Hex(main_nav_data) << "\","
               << "\"find_path_pointer\":\"" << Hex(find_path) << "\","
               << "\"find_path_pointer_executable\":" << (IsExecutable(find_path) ? "true" : "false") << ","
               << "\"default_filter_pointer\":\"" << Hex(default_filter_object) << "\","
               << "\"default_filter_pointer_readable\":" << (IsReadable(default_filter_object) ? "true" : "false") << ","
               << "\"default_filter_reference_controller\":\"" << Hex(default_filter_reference_controller) << "\","
               << "\"default_filter_impl\":\"" << Hex(default_filter_impl) << "\","
               << "\"default_filter_impl_readable\":" << (IsReadable(default_filter_impl) ? "true" : "false") << ","
               << "\"default_filter_impl_executable\":" << (IsExecutable(default_filter_impl) ? "true" : "false") << ","
               << "\"default_filter_max_search_nodes\":" << default_filter_max_search_nodes << ","
               << "\"recast_inventory\":{"
               << "\"nav_mesh_version\":" << recast_nav_mesh_version << ","
               << "\"impl_pointer\":\"" << Hex(recast_impl) << "\","
               << "\"impl_pointer_readable\":" << (IsReadable(recast_impl) ? "true" : "false") << ","
               << "\"scanned_impl_offset\":" << scanned_impl_offset << ","
               << "\"scanned_impl_pointer\":\"" << Hex(scanned_impl) << "\","
               << "\"scanned_impl_matches_owner\":" << (scanned_impl != nullptr ? "true" : "false") << ","
               << "\"impl_owner_pointer\":\"" << Hex(recast_impl_owner) << "\","
               << "\"detour_nav_mesh_pointer\":\"" << Hex(detour_nav_mesh) << "\","
               << "\"detour_nav_mesh_pointer_readable\":" << (IsReadable(detour_nav_mesh) ? "true" : "false")
               << "},"
               << "\"recast_config\":{\"tile_pool_size\":" << recast_tile_pool_size
               << ",\"tile_size_uu\":" << recast_tile_size
               << ",\"cell_size\":" << recast_cell_size
               << ",\"cell_height\":" << recast_cell_height
               << ",\"agent_radius\":" << recast_agent_radius
               << ",\"agent_height\":" << recast_agent_height
               << ",\"agent_max_slope\":" << recast_agent_slope << "},"
               << "\"recast_impl_words\":[";
        for (std::size_t index = 0; index < recast_impl_words.size(); ++index)
        {
            if (index != 0) report << ",";
            report << "\"" << Hex(reinterpret_cast<void*>(recast_impl_words[index])) << "\"";
        }
        report << "],"
               << "\"detour_inventory\":{"
               << "\"params_tile_width\":" << detour_params_tile_width << ","
               << "\"params_tile_height\":" << detour_params_tile_height << ","
               << "\"params_max_tiles\":" << detour_params_max_tiles << ","
               << "\"params_max_polys\":" << detour_params_max_polys << ","
               << "\"max_tiles\":" << detour_max_tiles << ","
               << "\"max_polys\":" << detour_max_polys << ","
               << "\"tile_lut_size\":" << detour_tile_lut_size << ","
               << "\"tile_lut_mask\":" << detour_tile_lut_mask << ","
               << "\"pos_lookup\":\"" << Hex(detour_pos_lookup) << "\","
               << "\"next_free\":\"" << Hex(detour_next_free) << "\","
               << "\"tiles\":\"" << Hex(detour_tiles) << "\","
               << "\"tiles_readable\":" << (IsReadable(detour_tiles, 0x20) ? "true" : "false") << ","
               << "\"salt_bits\":" << detour_salt_bits << ","
               << "\"tile_bits\":" << detour_tile_bits << ","
               << "\"poly_bits\":" << detour_poly_bits << ","
               << "\"tile_stride_candidates\":[";
        for (std::size_t index = 0; index < tile_stride_diagnostics.size(); ++index)
        {
            if (index != 0) report << ",";
            const auto& diagnostic = tile_stride_diagnostics[index];
            report << "{\"stride\":" << diagnostic.stride
                   << ",\"valid_tiles\":" << diagnostic.valid_tiles
                   << ",\"first_valid_index\":" << diagnostic.first_valid_index
                   << ",\"first_poly_count\":" << diagnostic.first_poly_count
                   << ",\"first_vert_count\":" << diagnostic.first_vert_count << "}";
        }
        report << "]},"
               << "\"tile_lookup_inventory\":{"
               << "\"lookup_size\":" << tile_lookup_inventory.lookup_size << ","
               << "\"pointer_entries_non_null\":" << tile_lookup_inventory.pointer_entries_non_null << ","
               << "\"valid_tiles\":" << tile_lookup_inventory.valid_tiles << ","
               << "\"samples\":[";
        for (std::size_t index = 0; index < tile_lookup_inventory.samples.size(); ++index)
        {
            if (index != 0) report << ",";
            const auto& sample = tile_lookup_inventory.samples[index];
            report << "{\"lookup_index\":" << sample.lookup_index
                   << ",\"tile\":\"" << Hex(sample.tile)
                   << "\",\"header\":\"" << Hex(sample.header)
                   << "\",\"version\":" << sample.version
                   << ",\"x\":" << sample.x
                   << ",\"y\":" << sample.y
                   << ",\"layer\":" << sample.layer
                   << ",\"poly_count\":" << sample.poly_count
                   << ",\"vert_count\":" << sample.vert_count << "}";
        }
        report << "],\"rejected_samples\":[";
        for (std::size_t index = 0; index < tile_lookup_inventory.rejected_samples.size(); ++index)
        {
            if (index != 0) report << ",";
            const auto& sample = tile_lookup_inventory.rejected_samples[index];
            report << "{\"lookup_index\":" << sample.lookup_index
                   << ",\"tile\":\"" << Hex(sample.tile) << "\",\"words\":[";
            for (std::size_t word_index = 0; word_index < sample.words.size(); ++word_index)
            {
                if (word_index != 0) report << ",";
                report << "{\"offset\":" << (word_index * sizeof(void*))
                       << ",\"readable\":" << (sample.word_readable[word_index] ? "true" : "false")
                       << ",\"value\":\""
                       << Hex(reinterpret_cast<void*>(sample.words[word_index])) << "\"}";
            }
            report << "],\"header_candidate\":\"" << Hex(sample.header_candidate)
                   << "\",\"header_readable\":" << (sample.header_readable ? "true" : "false")
                   << ",\"header_words\":[";
            for (std::size_t word_index = 0; word_index < sample.header_words.size(); ++word_index)
            {
                if (word_index != 0) report << ",";
                report << "\"" << std::hex << std::nouppercase << sample.header_words[word_index]
                       << std::dec << "\"";
            }
            report << "],\"header_minus_four_readable\":"
                   << (sample.header_minus_four_readable ? "true" : "false")
                   << ",\"header_minus_four_words\":[";
            for (std::size_t word_index = 0; word_index < sample.header_minus_four_words.size(); ++word_index)
            {
                if (word_index != 0) report << ",";
                report << "\"" << std::hex << std::nouppercase << sample.header_minus_four_words[word_index]
                       << std::dec << "\"";
            }
            report << "]}";
        }
        report << "],\"non_empty_buckets\":" << tile_lookup_inventory.non_empty_buckets
               << ",\"bucket_heads\":" << tile_lookup_inventory.bucket_heads
               << ",\"unique_active_tiles\":" << tile_lookup_inventory.unique_active_tiles
               << ",\"chained_tiles\":" << tile_lookup_inventory.chained_tiles
               << ",\"longest_chain\":" << tile_lookup_inventory.longest_chain
               << ",\"nodes_examined\":" << tile_lookup_inventory.nodes_examined
               << ",\"rejected_pointers\":" << tile_lookup_inventory.rejected_pointers
               << ",\"rejected_tile_headers\":" << tile_lookup_inventory.rejected_tile_headers
               << ",\"cyclic_pointers\":" << tile_lookup_inventory.cyclic_pointers
               << ",\"duplicate_pointers\":" << tile_lookup_inventory.duplicate_pointers
               << ",\"chain_bound_hits\":" << tile_lookup_inventory.chain_bound_hits
               << ",\"global_bound_hits\":" << tile_lookup_inventory.global_bound_hits
               << ",\"full_lookup_chain_traversed\":"
               << (tile_lookup_inventory.full_lookup_chain_traversed ? "true" : "false")
               << "},"
               << "\"default_filter_scan\":[";
        for (std::size_t index = 0; index < (sizeof(filter_scan_offsets) / sizeof(filter_scan_offsets[0])); ++index)
        {
            void* candidate = nullptr;
            ReadValue(main_nav_data, filter_scan_offsets[index], candidate);
            if (index != 0) report << ",";
            report << "{\"offset\":" << filter_scan_offsets[index]
                   << ",\"object\":\"" << Hex(candidate)
                   << "\",\"readable\":" << (IsReadable(candidate) ? "true" : "false") << "}";
        }
        report << "],";
        bool native_query_marker_consumed = false;
        if (native_query_requested && gameplay_pose_available)
        {
            // The marker is a one-shot capability grant. Consume it before
            // any native call so a crash or a later probe tick cannot replay
            // the query unexpectedly. If the pose is not ready, leave the
            // marker for the next bounded probe attempt.
            native_query_marker_consumed = ConsumeOneShotMarker(EnablePath);
        }

        PathQueryOutput path_query{};
        std::string path_query_json = "null";
        if (native_query_requested)
        {
            if (!gameplay_pose_available)
            {
                path_query.json = R"({"status":"deferred","reason_code":"player_position_unavailable_for_query"})";
            }
            else if (!native_query_marker_consumed)
            {
                path_query.json = R"({"status":"skipped","reason_code":"query_marker_not_consumed"})";
            }
            else if (!executable_identity.valid())
            {
                std::ostringstream skipped;
                skipped << "{\"status\":\"skipped\",\"reason_code\":\"known_executable_build_mismatch\","
                        << "\"filename_matches\":" << (executable_identity.filename_matches ? "true" : "false")
                        << ",\"sha256_matches\":" << (executable_identity.sha256_matches ? "true" : "false") << "}";
                path_query.json = skipped.str();
            }
            else
            {
                // FPathFindingQuery::Owner is the querier/source object, not
                // the UWorld. Use Cloud's live pawn when the verified
                // PlayerController->Pawn relationship is available; this is
                // the same object Unreal movement/path requests use for
                // filter and navigation-query context.
                auto* query_owner = player_pawn_object != nullptr ? player_pawn_object : world_object;
                path_query = RunNativePathQuery(main_nav_data, find_path,
                    static_cast<void*>(static_cast<std::uint8_t*>(main_nav_data) + NavigationDataDefaultFilterOffset),
                    query_owner);
            }
            path_query_json = path_query.json.empty() ? R"({"status":"skipped","reason_code":"query_not_executed"})" : path_query.json;
        }
        report
               << "\"native_path_query_requested\":" << (native_query_requested ? "true" : "false") << ","
               << "\"native_path_query_marker_consumed\":" << (native_query_marker_consumed ? "true" : "false") << ","
               << "\"native_path_query_called\":" << (path_query.called ? "true" : "false") << ","
               << "\"native_geometry_marker_present\":" << (geometry_marker_present ? "true" : "false") << ","
               << "\"native_geometry_readiness_present\":" << (geometry_readiness_present ? "true" : "false") << ","
               << "\"native_geometry_requested\":" << (geometry_requested ? "true" : "false") << ","
               << "\"native_geometry_marker_consumed\":" << (geometry_marker_consumed ? "true" : "false") << ","
               << "\"native_geometry_dump_written\":" << (geometry_dump_written ? "true" : "false") << ","
               << "\"native_query_api\":\"ANavigationData::FindPath-style implementation\","
               << "\"executable_identity\":{\"path\":\"" << Escape(executable_identity.path)
               << "\",\"sha256\":\"" << executable_identity.sha256
               << "\",\"known_sha256\":\"" << KnownGameSha256
               << "\",\"filename_matches\":" << (executable_identity.filename_matches ? "true" : "false")
               << ",\"sha256_matches\":" << (executable_identity.sha256_matches ? "true" : "false")
               << ",\"valid\":" << (executable_identity.valid() ? "true" : "false") << "},"
               << "\"path_query\":" << path_query_json << ","
               << "\"offsets\":{\"engine_game_viewport\":" << EngineGameViewportOffset
               << ",\"game_viewport_world\":" << GameViewportWorldOffset
               << ",\"controller_pawn\":" << ControllerPawnOffset
               << ",\"world_navigation_system\":" << WorldNavigationSystemOffset
                << ",\"navigation_main_nav_data\":" << NavigationMainNavDataOffset
                << ",\"navigation_data_find_path\":" << NavigationDataFindPathOffset
                << ",\"navigation_data_default_filter\":" << NavigationDataDefaultFilterOffset
                << ",\"recast_nav_mesh_version\":" << RecastNavMeshVersionOffset
                << ",\"recast_nav_mesh_impl\":" << RecastNavMeshImplOffset
                << ",\"recast_nav_mesh_impl_owner\":" << RecastNavMeshImplOwnerOffset
                << ",\"recast_nav_mesh_impl_detour\":" << RecastNavMeshImplDetourOffset
               << ",\"navigation_data_supported_areas\":" << NavigationDataSupportedAreasOffset << "},"
               << "\"movement_sent\":false}";
        WriteAuthoredRouteReport(BuildAuthoredRouteInventory(world_object));
        const auto report_serialization_started = std::chrono::steady_clock::now();
        const auto serialized_report = report.str();
        LastProbeTiming.probe_report_json_serialization_us =
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - report_serialization_started).count();
        return serialized_report;
    }

    class NativeNavigationProbe final : public RC::CppUserModBase
    {
    public:
        NativeNavigationProbe()
        {
            ModName = STR("FF7RNavigationNativeProbe");
            ModVersion = STR("0.1.0");
            ModAuthors = STR("Codex");
            ModDescription = STR("Read-only native navigation ABI/object discovery; movement disabled");
        }

        void on_update() override
        {
            if (!unreal_ready_)
            {
                return;
            }
            // These are intentionally cheap filesystem checks. In
            // particular, a stale geometry request marker without the
            // post-render readiness token must not trigger BuildReport or
            // keep the expensive retry loop alive through loading.
            const bool query_requested = std::filesystem::exists(EnablePath);
            const bool geometry_marker_present = std::filesystem::exists(GeometryEnablePath);
            const bool geometry_ready = std::filesystem::exists(GeometryReadyPath);
            const bool geometry_armed = geometry_marker_present && geometry_ready;
            // Stay fully dormant by default. A geometry marker is only an
            // expensive grant when its separate settled-render token is also
            // present; a native query marker remains an explicit diagnostic
            // opt-in. These checks are intentionally before reported_ and
            // before the probe-period/BuildReport path.
            if (!query_requested && !geometry_armed)
            {
                return;
            }
            if (reported_)
            {
                // A later readiness token is a new explicit capture arm. It
                // is the only way to wake a completed probe after a prior
                // loading-boundary refusal.
                if (!query_requested && !geometry_armed)
                {
                    return;
                }
                reported_ = false;
            }
            if (geometry_marker_present && !geometry_ready && !query_requested)
            {
                return;
            }
            const auto now = GetTickCount64();
            if (now - last_probe_ms_ < ProbePeriodMs)
            {
                return;
            }
            last_probe_ms_ = now;
            ++probe_attempts_;
            LastProbeTiming = {};
            LastProbeTiming.probe_attempt = probe_attempts_;
            LastProbeTiming.geometry_armed = geometry_armed;
            if (geometry_armed)
            {
                CaptureRequest request;
                if (!ReadCaptureRequest(request))
                {
                    if (!request.request_id.empty())
                    {
                        request.valid = true;
                        ActiveCaptureRequest = request;
                        ActiveCaptureStarted = std::chrono::steady_clock::now();
                        ActiveCaptureStatusWritten = false;
                        WriteCaptureStatus("failed", "capture_request_invalid");
                    }
                    ConsumeOneShotMarker(GeometryEnablePath);
                    ConsumeOneShotMarker(GeometryReadyPath);
                    reported_ = true;
                    ActiveCaptureRequest = {};
                    ActiveCaptureStatusWritten = false;
                    return;
                }
                ActiveCaptureRequest = request;
                ActiveCaptureStarted = std::chrono::steady_clock::now();
                ActiveCaptureStatusWritten = false;
                LastProbeTiming.request_id = request.request_id;
                if (!WriteCaptureStatus("accepted"))
                {
                    ConsumeOneShotMarker(GeometryEnablePath);
                    ConsumeOneShotMarker(GeometryReadyPath);
                    reported_ = true;
                    ActiveCaptureRequest = {};
                    ActiveCaptureStatusWritten = false;
                    return;
                }
            }
            WriteCaptureStatus("build_report_started");
            const auto build_started = std::chrono::steady_clock::now();
            std::string report;
            try
            {
                report = BuildReport();
            }
            catch (const std::exception&)
            {
                if (geometry_armed)
                {
                    WriteCaptureStatus("failed", "capture_native_failed");
                    ConsumeOneShotMarker(GeometryReadyPath);
                    ActiveCaptureRequest = {};
                    ActiveCaptureStatusWritten = false;
                }
                reported_ = true;
                return;
            }
            LastProbeTiming.full_build_report_us =
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - build_started).count();
            LastProbeTiming.geometry_dump_written =
                report.find("\"native_geometry_dump_written\":true") != std::string::npos;
            const auto report_write_started = std::chrono::steady_clock::now();
            const bool report_written = WriteReport(AddCaptureRequestMetadata(report));
            LastProbeTiming.probe_json_write_us =
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - report_write_started).count();
            if (geometry_armed && report_written)
            {
                WriteCaptureStatus("probe_written");
            }
            // Consume the render-settled token after exactly one expensive
            // attempt. A pending geometry marker therefore cannot cause
            // repeated BuildReport calls during loading; the next settled
            // route preflight must arm a fresh token.
            if (geometry_armed)
            {
                ConsumeOneShotMarker(GeometryReadyPath);
            }
            const bool navigation_objects_available =
                report.find("\"status\":\"navigation_native_objects_available\"") != std::string::npos;
            const bool query_called = report.find("\"native_path_query_called\":true") != std::string::npos;
            // When the explicit query marker is present, keep sampling until
            // the state exporter has supplied a player position and the
            // guarded native call has actually happened.
            const bool query_finished = !query_requested || query_called;
            const bool geometry_requested = std::filesystem::exists(GeometryEnablePath);
            const bool geometry_written =
                report.find("\"native_geometry_dump_written\":true") != std::string::npos;
            // A geometry marker may remain present after this one-shot arm,
            // but the readiness token has already been consumed. Do not keep
            // serializing reports across a menu/loading boundary; a later
            // settled preflight must publish a fresh readiness token.
            const bool geometry_finished = !geometry_requested || geometry_written;
            reported_ = (navigation_objects_available && query_finished && geometry_finished) ||
                        geometry_armed ||
                        probe_attempts_ >= MaximumProbeAttempts;
            if (geometry_armed)
            {
                const bool timing_written = WriteTimingReport(LastProbeTiming);
                const bool capture_outputs_complete = navigation_objects_available
                    && geometry_written
                    && report_written
                    && timing_written;
                if (capture_outputs_complete)
                {
                    // BuildReport does not contain the geometry JSON. Use the
                    // exporter's completion result, never search the probe report.
                    const char* artifact_status = LastProbeTiming.geometry_export_complete
                        ? "complete"
                        : "partial_or_incomplete";
                    WriteCaptureStatus("completed", nullptr, artifact_status);
                }
                else if (!navigation_objects_available)
                {
                    WriteCaptureStatus("failed", "capture_native_failed");
                }
                else
                {
                    WriteCaptureStatus("failed", "capture_output_incomplete");
                }
                ActiveCaptureRequest = {};
                ActiveCaptureStatusWritten = false;
            }
            else
            {
                WriteTimingReport(LastProbeTiming);
            }
        }

        void on_unreal_init() override
        {
            unreal_ready_ = true;
            last_probe_ms_ = GetTickCount64() - ProbePeriodMs;
        }

    private:
        bool unreal_ready_{false};
        bool reported_{false};
        ULONGLONG last_probe_ms_{0};
        std::uint32_t probe_attempts_{0};
    };
}

#define MOD_EXPORT __declspec(dllexport)
extern "C"
{
    MOD_EXPORT RC::CppUserModBase* start_mod()
    {
        return new NativeNavigationProbe();
    }

    MOD_EXPORT void uninstall_mod(RC::CppUserModBase* mod)
    {
        delete mod;
    }
}
