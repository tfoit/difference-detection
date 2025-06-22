from cp_utils.config import get_config
from cp_utils.clogging import logger, logged
from cp_utils.entity import DocResult, LabelItem, Entity, EntityType, Location
import numpy as np
from cp_credit.appconf import ApplicationConfig, CaseDocumentDifferencesConfig
from cp_credit.pipeline.components import Component
from cp_credit.pipeline.pipeline_types import EvaluationPipelineMessage
from cp_credit.remote.visual_difference import visual_difference
from difflib import SequenceMatcher

excluded_locations = [
    Location(
        top=0.025,
        height=0.04,
        left=0.165,
        width=0.125
    )
]

# Configuration for difference detection thresholds
DIFFERENCE_CONFIG = {
    'min_difference_area': 0.0001,  # Minimum area (as fraction of page) for a difference to be considered
    'min_difference_width': 0.005,  # Minimum width for meaningful differences
    'min_difference_height': 0.005,  # Minimum height for meaningful differences
    'exclusion_intersection_threshold': 0.7,  # Increased from 0.5 to be more aggressive in excluding
    'text_similarity_threshold': 0.75,  # Slightly lowered from 0.8 for OCR variations
    'max_differences_per_page': 20,  # Limit to prevent artifact spam
    'confidence_threshold': 0.6  # Minimum confidence for visual differences
}

class CaseDocumentDifferences(Component):
    name = "case_document_differences"
    provides = []
    requires = ["image"]
    defaults = {}

    def __init__(self, component_config: CaseDocumentDifferencesConfig):
        super().__init__(component_config)
        self.config = get_config(ApplicationConfig)

    @logged(component_name=name)
    async def process(self, message: EvaluationPipelineMessage, **kwargs):
        success_label = LabelItem(label=f"{self.name}", entities=[])
        exclude_label = LabelItem(label=f"{self.name}_excluded", entities=[])
        error_label = LabelItem(label=f"{self.name}_error", entities=[])

        error_message = ""
        validate_data_exists(message, error_label)
        ocr_difference(message, error_label, success_label)

        await visual_differences(message, error_message, exclude_label, success_label, error_label)
        success = not bool(success_label.entities and not error_label.entities)
        results = [success_label, error_label, exclude_label]

        doc_result = DocResult(success=success, message=error_message, results=results, module=self.name)
        message.entity_annotation.append(doc_result)

        # Log summary statistics for debugging
        total_visual_differences = len([e for e in success_label.entities if e.entity.startswith("case_document_visual_difference_")])
        total_ocr_differences = len([e for e in success_label.entities if e.entity.startswith("case_document_differences_ocr_")])
        total_excluded = len(exclude_label.entities)
        
        logger.info(f"Finished {self.name} Component - Visual: {total_visual_differences}, OCR: {total_ocr_differences}, Excluded: {total_excluded}")
        
        if total_visual_differences > 10:
            logger.warning(f"High number of visual differences detected ({total_visual_differences}). Consider adjusting thresholds if these are false positives.")

async def visual_differences(message, error_message, exclude_label, success_label, error_label):
    original_images = message.original_images
    images = message.images
    if images and original_images:
        try:
            excluded_difference_locations = get_excluded_difference_locations(message)
            for index, excluded_location in enumerate(excluded_difference_locations):
                excluded_location_id = index + 1
                exclude_label.entities.append(
                    Entity(
                        entity=f"difference_excluded_{excluded_location_id}",
                        value=True,
                        valueClean=True,
                        location=excluded_location,
                        type=EntityType.BOOLEAN
                    )
                )

            context = message.context
            differences = await get_differences(images, original_images, context, excluded_difference_locations)

            # Filter and validate differences to reduce false positives
            filtered_differences = filter_meaningful_differences(differences)
            
            logger.info(f"Found {len(differences)} raw differences, filtered to {len(filtered_differences)} meaningful differences")

            for index, difference in enumerate(filtered_differences):
                difference_id = index + 1
                if difference.top + difference.height > 1:
                    difference.height = 1 - difference.top
                if difference.width + difference.left > 1:
                    difference.width = 1 - difference.left
                success_label.entities.append(
                    Entity(
                        entity=f"case_document_visual_difference_{difference_id}",
                        value=True,
                        valueClean=True,
                        location=difference,
                        type=EntityType.BOOLEAN
                    )
                )
        except Exception as exception:
            error_message = f"Unable to identify differences. Exception: {str(exception)}"
            logger.warning(error_message)
            error_label.entities.append(
                Entity(
                    entity="case_document_visual_differences_exception",
                    value=error_message,
                    valueClean=error_message,
                    type=EntityType.TEXT
                )
            )
    else:
        error_message = f"Images missing. Document: {len(images)}, Original Document: {len(original_images) if original_images else 0}"
        logger.warning(error_message)
        error_label.entities.append(
            Entity(
                entity="case_document_visual_images_missing",
                value=error_message,
                valueClean=error_message,
                type=EntityType.TEXT
            )
        )

def filter_meaningful_differences(differences):
    """
    Filter out noise and artifacts to keep only meaningful differences
    """
    filtered = []
    
    # Group differences by page for per-page filtering
    page_differences = {}
    for diff in differences:
        page = getattr(diff, 'page', 1)
        if page not in page_differences:
            page_differences[page] = []
        page_differences[page].append(diff)
    
    for page, page_diffs in page_differences.items():
        # Sort by area (largest first) to prioritize significant differences
        page_diffs.sort(key=lambda d: d.width * d.height, reverse=True)
        
        # Limit number of differences per page to prevent artifact spam
        page_diffs = page_diffs[:DIFFERENCE_CONFIG['max_differences_per_page']]
        
        for diff in page_diffs:
            # Calculate difference area
            area = diff.width * diff.height
            
            # Filter by minimum size thresholds
            if (area >= DIFFERENCE_CONFIG['min_difference_area'] and 
                diff.width >= DIFFERENCE_CONFIG['min_difference_width'] and 
                diff.height >= DIFFERENCE_CONFIG['min_difference_height']):
                
                # Check if difference has sufficient confidence (if available)
                confidence = getattr(diff, 'confidence', 1.0)
                if confidence >= DIFFERENCE_CONFIG['confidence_threshold']:
                    filtered.append(diff)
                else:
                    logger.debug(f"Filtered out low confidence difference: {confidence}")
            else:
                logger.debug(f"Filtered out small difference: area={area}, width={diff.width}, height={diff.height}")
    
    return filtered

async def get_differences(images, original_images, context, locations_to_exclude):
    differences = []
    difference_result = await visual_difference(images, original_images, context, '-')
    for page_result in difference_result:
        if type(page_result) is not list:
            break
        for difference in page_result:
            # Use more aggressive exclusion threshold
            if all(difference.intersection_percentage(l) < DIFFERENCE_CONFIG['exclusion_intersection_threshold'] for l in locations_to_exclude):
                differences.append(difference)
            else:
                logger.debug(f"Excluded difference due to intersection with excluded location")
    return differences

def get_excluded_difference_locations(message):
    locations_to_exclude = []
    
    # Expand barcode exclusion areas more aggressively
    for barcode in message.barcodes:
        location = barcode.location
        extended_barcode_location = Location(
            left=location.left - 0.02,  # Increased from 0.01
            top=location.top - 0.02,   # Increased from 0.01
            width=location.width + 0.04,  # Increased from 0.02
            height=location.height + 0.04,  # Increased from 0.02
            page=location.page
        )
        locations_to_exclude.append(extended_barcode_location)

        banking_relationship_location = Location(
            left=location.left - 0.18,  # Increased from 0.16
            top=location.top,
            width=0.18,  # Increased from 0.16
            height=0.03,  # Increased from 0.02
            page=location.page
        )
        locations_to_exclude.append(banking_relationship_location)

    # Add standard exclusion areas with larger buffers
    for index, _ in enumerate(message.images):
        page_number = index + 1
        for excluded_location in excluded_locations:
            locations_to_exclude.append(Location(
                left=excluded_location.left - 0.03,  # Increased from 0.02
                top=excluded_location.top - 0.03,   # Increased from 0.02
                width=excluded_location.width + 0.06,  # Increased from 0.04
                height=excluded_location.height + 0.06,  # Increased from 0.04
                page=page_number
            ))
        
        # Standard exclusion areas with larger buffers
        locations_to_exclude.append(Location(left=0.70, top=0.08, width=0.16, height=0.06, page=page_number))  # Expanded
        locations_to_exclude.append(Location(left=0.39, top=0.10, width=0.20, height=0.06, page=page_number))  # Expanded

    # Signature line exclusions with larger areas
    for line in message.horizontal_line_regions:
        above_line_location = Location(
            left=line.left - 0.05,  # Increased from 0.03
            top=line.top - 0.02,    # Increased from 0.01
            width=line.right - line.left + 0.10,  # Increased from 0.06
            height=0.06,  # Increased from 0.047
            page=2
        )
        if any(sig.signature.location.intersection_percentage(above_line_location) > 0.6 for sig in message.extracted_signatures):
            locations_to_exclude.append(above_line_location)

    return locations_to_exclude

def validate_data_exists(message, error_label):
    images = message.images
    if not images:
        error_label.entities.append(Entity(
            entity="case_document_differences_ocr_missing_document_images",
            value=True,
            valueClean=True,
            type=EntityType.BOOLEAN
        ))

    original_images = message.original_images
    if not original_images:
        error_label.entities.append(Entity(
            entity="case_document_differences_ocr_missing_case_document_images",
            value=True,
            valueClean=True,
            type=EntityType.BOOLEAN
        ))

    if len(original_images) != len(images):
        error_label.entities.append(Entity(
            entity="case_document_differences_ocr_page_count_mismatch",
            value=True,
            valueClean=True,
            type=EntityType.BOOLEAN
        ))

    tsv = message.tsv
    if tsv is None or tsv.empty:
        error_label.entities.append(Entity(
            entity="case_document_differences_ocr_missing_document_ocr",
            value=True,
            valueClean=True,
            type=EntityType.BOOLEAN
        ))

    original_tsv = message.original_tsv
    if original_tsv is None or original_tsv.empty:
        error_label.entities.append(Entity(
            entity="case_document_differences_ocr_missing_case_document_ocr",
            value=True,
            valueClean=True,
            type=EntityType.BOOLEAN
        ))

def check_differences_by_locations(message):
    missing = []
    added = []
    text_differences = []
    original_images = message.original_images

    for index, original_image in enumerate(original_images):
        page_number = index + 1
        original_tsv = message.original_tsv
        original_tsv = original_tsv[(original_tsv['page_num'] == page_number) & (original_tsv['level'] == 4)]
        tsv = message.tsv
        tsv = tsv[(tsv['page_num'] == page_number) & (tsv['level'] == 4)]
        tsv.loc[:, 'matched'] = False

        for idx, original_row in original_tsv.iterrows():
            original_location = Location(
                left=original_row['location_left'],
                top=original_row['location_top'],
                width=original_row['location_width'],
                height=original_row['location_height'],
                page=page_number
            )

            exclude = False
            for exclude_location in get_excluded_difference_locations(message):
                if exclude_location.page != page_number:
                    continue
                if original_location.intersection_percentage(exclude_location) > 0.5:
                    exclude = True
                    break

            if exclude:
                continue

            match_found = False
            for jdx, tsv_row in tsv.iterrows():
                tsv_location = Location(
                    left=tsv_row['location_left'],
                    top=tsv_row['location_top'],
                    width=tsv_row['location_width'],
                    height=tsv_row['location_height'],
                    page=page_number
                )
                intersect = original_location.intersection_percentage(tsv_location)
                if intersect > 0.0:
                    match_found = True
                    tsv.at[jdx, 'matched'] = True
                    if not is_text_similar(original_row['text'], tsv_row['text']):
                        original_row['difference'] = f"Different Text: {original_row['text']}. Document: {tsv_row['text']}"
                        text_differences.append(original_row)
                    break

            if not match_found:
                original_row['difference'] = f"Missing Text: {original_row['text']}"
                missing.append(original_row)

        for idx, tsv_row in tsv.iterrows():
            if not tsv_row['matched']:
                tsv_row['difference'] = f"Added Text: {tsv_row['text']}"
                added.append(tsv_row)

    return missing + added + text_differences

def ocr_difference(message, error_label, success_label):
    if not error_label.entities:
        differences = check_differences_by_locations(message)
        for difference_index, difference in enumerate(differences):
            location = Location(
                left=difference['location_left'],
                top=difference['location_top'],
                width=difference['location_width'],
                height=difference['location_height'],
                page=difference['page_num']
            )
            entity = Entity(
                entity=f"case_document_differences_ocr_{difference_index + 1}",
                value=True,
                valueClean=True,
                location=location,
                type=EntityType.BOOLEAN
            )
            success_label.entities.append(entity)

def is_text_similar(text1, text2, threshold=None):
    """
    Check if two text strings are similar, accounting for OCR variations
    """
    if threshold is None:
        threshold = DIFFERENCE_CONFIG['text_similarity_threshold']
    
    # Handle empty strings
    if not text1 and not text2:
        return True
    if not text1 or not text2:
        return False
    
    # Clean text for comparison (remove extra whitespace, normalize case)
    clean_text1 = ' '.join(text1.strip().split()).lower()
    clean_text2 = ' '.join(text2.strip().split()).lower()
    
    # Use sequence matcher for similarity
    similarity = SequenceMatcher(None, clean_text1, clean_text2).ratio()
    
    # Additional checks for common OCR variations
    if similarity < threshold:
        # Check for common OCR character substitutions
        ocr_variants = {
            'o': '0', '0': 'o', 'i': '1', '1': 'i', 'l': '1', '1': 'l',
            's': '5', '5': 's', 'g': '9', '9': 'g', 'b': '6', '6': 'b'
        }
        
        # Create variants by substituting common OCR errors
        variant_text1 = clean_text1
        for original, replacement in ocr_variants.items():
            variant_text1 = variant_text1.replace(original, replacement)
        
        variant_similarity = SequenceMatcher(None, variant_text1, clean_text2).ratio()
        similarity = max(similarity, variant_similarity)
    
    return similarity > threshold

def update_difference_config(**kwargs):
    """
    Utility function to update difference detection configuration at runtime
    Usage: update_difference_config(min_difference_area=0.0002, exclusion_intersection_threshold=0.8)
    """
    global DIFFERENCE_CONFIG
    for key, value in kwargs.items():
        if key in DIFFERENCE_CONFIG:
            old_value = DIFFERENCE_CONFIG[key]
            DIFFERENCE_CONFIG[key] = value
            logger.info(f"Updated {key}: {old_value} -> {value}")
        else:
            logger.warning(f"Unknown configuration key: {key}")

def get_difference_config():
    """
    Get current difference detection configuration
    """
    return DIFFERENCE_CONFIG.copy()
